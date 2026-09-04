"""Which session is which, once the box has more than one radio.

MEASURED 2026-09-04: with APRS logging on the long wire and the tuner on the desk whip,
the sidecar holds two sessions. `listening` is now the ONE the omnibox should draw — the
tuner, by preference — so every caller that asked it "is APRS logging?" started answering
no while it was running. Three callers asked exactly that: the PWA's APRS routes, jerv's
`sdr_aprs_logging`, and the packet drain, which would DETACH and drop frames the radio
was still decoding.
"""

from __future__ import annotations

from typing import Any

from jbrain.sdr.health import session_for


def _two_radios() -> dict[str, Any]:
    """What a two-dongle box reports: the tuner is `listening`, APRS is not."""
    return {
        "purposes": ["listen", "aprs", "survey"],
        "listening": {"purpose": "listen", "session_id": "s-tuner", "serial": "09022796"},
        "sessions": [
            {"purpose": "listen", "session_id": "s-tuner", "serial": "09022796"},
            {"purpose": "aprs", "session_id": "s-aprs", "serial": "77192819"},
        ],
    }


def test_it_finds_the_session_listening_did_not_name() -> None:
    assert session_for(_two_radios(), "aprs")["session_id"] == "s-aprs"


def test_it_still_finds_the_one_listening_did_name() -> None:
    assert session_for(_two_radios(), "listen")["session_id"] == "s-tuner"


def test_a_job_nothing_is_doing_is_falsy_rather_than_None() -> None:
    """So a caller asks `if session:` rather than comparing a purpose a second time —
    the second comparison is where the old bug lived."""
    assert session_for(_two_radios(), "survey") == {}


def test_an_older_sidecar_is_read_through_listening() -> None:
    """The api and the sidecar are separate containers and an update restarts them one
    at a time, so for a few seconds one of them is the previous build. That build has at
    most one session, so `listening` is exactly right there."""
    old = {"purposes": ["listen", "aprs"], "listening": {"purpose": "aprs", "session_id": "s1"}}

    assert session_for(old, "aprs")["session_id"] == "s1"
    assert session_for(old, "listen") == {}


def test_an_older_sidecar_holding_nothing_reads_as_nothing() -> None:
    assert session_for({"purposes": ["listen"], "listening": None}, "aprs") == {}


def test_an_unreachable_sidecar_is_not_a_crash() -> None:
    """`_health` returns None on any transport error, and every caller passes that
    straight in."""
    assert session_for(None, "aprs") == {}


def test_a_malformed_payload_degrades_rather_than_raising() -> None:
    """This runs on the drain's poll loop and on a route the PWA polls. A sidecar
    answering something unexpected must cost an answer, not the loop."""
    for junk in ({"sessions": "nope"}, {"sessions": ["a", 5, None]}, {}, {"sessions": []}):
        assert session_for(junk, "aprs") == {}


class TestWhatTheSidecarIsRunningOn:
    """`resolve.busy_serials`, which turns "which radio is free" from a guess into a
    reading. Without it `choose` handed APRS and the tuner the same `generals[0]`."""

    def _sidecar(self, payload: Any, status: int = 200) -> Any:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            if payload is None:
                raise httpx.ConnectError("no sidecar")
            return httpx.Response(status, json=payload)

        real = httpx.AsyncClient

        def build(*args: Any, **kwargs: Any) -> Any:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        return build

    async def _busy(self, monkeypatch: Any, payload: Any) -> list[str]:
        import httpx

        from jbrain.sdr import resolve

        monkeypatch.setattr(httpx, "AsyncClient", self._sidecar(payload))
        return await resolve.busy_serials("http://sdr:8000")

    async def test_it_names_every_radio_with_a_session_on_it(self, monkeypatch: Any) -> None:
        busy = await self._busy(
            monkeypatch,
            {
                "sessions": [
                    {"purpose": "listen", "serial": "77192819"},
                    {"purpose": "aprs", "serial": "09022796"},
                ]
            },
        )

        assert busy == ["09022796", "77192819"]

    async def test_a_session_naming_no_radio_is_not_a_busy_serial(self, monkeypatch: Any) -> None:
        """A one-dongle box names nothing, and there is nothing to reorder there. The
        sidecar refuses that case on its own — an unnamed session conflicts with
        everything — so inventing a serial here would only be wrong."""
        assert await self._busy(monkeypatch, {"sessions": [{"purpose": "listen"}]}) == []

    async def test_an_older_sidecar_is_read_through_listening(self, monkeypatch: Any) -> None:
        payload = {"listening": {"purpose": "aprs", "serial": "77192819"}}

        assert await self._busy(monkeypatch, payload) == ["77192819"]

    async def test_an_unreachable_sidecar_reports_nothing_busy_rather_than_everything(
        self, monkeypatch: Any
    ) -> None:
        """Guessing "all busy" would turn a transport error into "no radio available" —
        a settings problem the owner does not have. Nothing can start anyway."""
        assert await self._busy(monkeypatch, None) == []

    async def test_a_box_with_no_sidecar_is_not_asked(self, monkeypatch: Any) -> None:
        from jbrain.sdr import resolve

        assert await resolve.busy_serials(None) == []
        assert await resolve.busy_serials("") == []
