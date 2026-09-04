"""The three routes behind the live waterfall.

What is worth testing here is not "does it proxy" — it is the arithmetic and the
refusals, because both are things the owner meets as a picture that is wrong rather
than as an error. A one-hop band has to get the bin width its own capture produces, a
span too wide for one capture has to fall back to the tool that can hop, a refusal has
to arrive as a sentence, and moving the picture to another band must never release the
radio in between — that window is how a waterfall disappears because someone changed
band.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import sdr as sdr_api
from jbrain.sdr import bands
from jbrain.sdr.roles import Radio
from jbrain.sdr.tuner import live_bin_hz

# Typed `Any` so the fakes can stand in for `SettingsDep`/`OwnerDep` without a
# `type: ignore` on every call — the routes read two attributes off each.
OWNER: Any = SimpleNamespace(id="owner", kind="owner")


class _Settings:
    sdr_url = "http://sdr:8000"
    supervisor_token = "t"


def _settings() -> Any:
    return _Settings()


def _request(disconnected: bool = False) -> Any:
    async def is_disconnected() -> bool:
        return disconnected

    return SimpleNamespace(
        is_disconnected=is_disconnected,
        app=SimpleNamespace(state=SimpleNamespace()),
    )


# --- the range ------------------------------------------------------------------


def test_a_one_hop_section_brings_the_bin_width_ITS_capture_produces() -> None:
    """Not a number anyone typed and not one rtl_power granted: `air-tower` is captured
    at 2.4 MS/s over 4000 bins, so a bin is 600 Hz EXACTLY. The pairing is chosen for
    that (`bands.LIVE_CAPTURES`) — at 4096 bins the same rate is 585.9375 Hz, and a
    frame rounding that to 586 is 256 Hz out at its top edge with nothing able to tell.
    """
    section = bands.by_id("air-tower")
    assert section is not None

    start, stop, bin_hz = sdr_api._span("air-tower", None, None)

    assert (start, stop) == (section.start_hz, section.stop_hz)
    assert (section.sample_rate_hz, section.fft_bins) == (2_400_000, 4_000)
    assert bin_hz == 600
    assert isinstance(bin_hz, int)  # exact, so `sameBand` can compare it


def test_a_multi_hop_section_still_gets_rtl_powers_own_ladder() -> None:
    """20 MHz of FM broadcast is more than one capture, so it is still swept — hops, one
    row a second, and a bin width the tool picks. Keeping that here is the point of
    splitting the two tiers rather than pretending one engine serves both."""
    section = bands.by_id("fm-broadcast")
    assert section is not None
    assert section.sample_rate_hz == 0

    _start, _stop, bin_hz = sdr_api._span("fm-broadcast", None, None)

    assert bin_hz == live_bin_hz(section.span_hz, section.sweep_bin_hz)


def test_an_explicit_range_takes_the_same_ladder_a_section_does() -> None:
    """The expert path and the band button must produce the SAME picture, or the width
    of a bin depends on how the owner asked for the band."""
    ssb = bands.by_id("2m-ssb")
    assert ssb is not None

    _start, _stop, typed = sdr_api._span(None, 144.1, 144.3)

    assert typed == ssb.live_bin_hz == 250


def test_a_hand_entered_range_too_wide_for_one_capture_falls_back_to_the_tool() -> None:
    _start, _stop, bin_hz = sdr_api._span(None, 144.0, 148.0)

    assert bin_hz == live_bin_hz(4_000_000, sdr_api.DEFAULT_SPECTRUM_BIN_HZ)


def test_shortwave_is_drawn_rather_than_refused() -> None:
    """F8. It used to come back "a sweep cannot go below 24 MHz" — true of rtl_power and
    of nothing else. The live engine reads raw I/Q and sets direct sampling mode 2, so
    40 m is a picture now, at the width its own capture makes."""
    forty = bands.by_id("40m")
    assert forty is not None

    start, stop, bin_hz = sdr_api._span("40m", None, None)

    assert (start, stop) == (7_125_000, 7_300_000)
    assert bin_hz == 250  # 256 kS/s over 1024 bins


def test_shortwave_wider_than_one_capture_is_refused_in_words() -> None:
    """The one surface an owner with no terminal has (CLAUDE.md #10). The refusal down
    here is narrower than it was, and it has to say which limit it hit."""
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, 3.0, 8.0)

    assert raised.value.status_code == 400
    assert "more than one capture" in str(raised.value.detail)


def test_a_span_wider_than_the_radio_can_sweep_says_so() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, 400.0, 500.0)

    assert raised.value.status_code == 400
    assert "Pick a section" in str(raised.value.detail)


def test_a_section_that_does_not_exist_is_a_404() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span("no-such-band", None, None)

    assert raised.value.status_code == 404


def test_a_waterfall_with_no_range_at_all_is_refused() -> None:
    with pytest.raises(HTTPException) as raised:
        sdr_api._span(None, None, None)

    assert raised.value.status_code == 400
    assert "band section" in str(raised.value.detail)


# --- starting and moving --------------------------------------------------------


def _posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def post(_settings: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append((path, body))
        return {"session_id": "s1", "purpose": "spectrum"}

    async def radio_for(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(
            serial="77192819", radio=Radio(serial="77192819"), conflict=None, refusal=None
        )

    monkeypatch.setattr(sdr_api, "_post", post)
    monkeypatch.setattr(sdr_api, "_radio_for", radio_for)
    monkeypatch.setattr(sdr_api, "_refuse", lambda _c: None)
    return seen


async def test_starting_names_the_radio_and_the_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _posts(monkeypatch)

    await sdr_api.spectrum_start(
        _request(),
        _settings(),
        OWNER,
        section="fm-broadcast",
    )

    path, body = seen[0]
    assert path == "/listen/start"
    assert body["purpose"] == "spectrum"
    assert body["serial"] == "77192819"
    assert body["start_hz"] == 88_000_000
    # No `frequency_hz` is sent at all: a span's centre is the only frequency it has,
    # and the sidecar is the one place that derives it.
    assert "frequency_hz" not in body


async def test_moving_the_picture_never_releases_the_radio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window this route exists to close: stop-then-start hands the dongle to
    whatever asks next, and the owner's waterfall vanishes because they changed band."""
    seen = _posts(monkeypatch)

    await sdr_api.spectrum_tune(
        _settings(),
        OWNER,
        section="air-tower",
        session_id="s1",
    )

    assert [path for path, _body in seen] == ["/listen/tune"]
    assert seen[0][1]["session_id"] == "s1"


# --- the stream -----------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, lines: list[str] | None = None, body: bytes = b"") -> None:
        self.status_code = status
        self._lines = lines or []
        self._body = body

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _Stream:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _Resp:
        return self._resp

    async def __aexit__(self, *_a: Any) -> bool:
        return False


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def stream(self, _method: str, _path: str) -> _Stream:
        return _Stream(self._resp)

    async def aclose(self) -> None:
        return None


def _upstream(monkeypatch: pytest.MonkeyPatch, resp: _Resp) -> None:
    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", lambda **_kw: _Client(resp))


async def _collect(resp_out: Any) -> list[str]:
    return [chunk async for chunk in resp_out.body_iterator]


async def test_a_row_is_relayed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """This route understands nothing about the picture, and that is the design: each
    row already says which band it covers, so a retune lands with no message here."""
    row = json.dumps({"start_hz": 88_000_000, "bin_hz": 25_000, "db": [-70.0]})
    _upstream(monkeypatch, _Resp(200, [row, "", '{"keepalive":true}']))

    out = await sdr_api.spectrum(_request(), _settings(), OWNER)

    assert await _collect(out) == [f"data: {row}\n\n", 'data: {"keepalive":true}\n\n']


async def test_the_sidecars_own_refusal_is_not_buried_in_a_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sidecar's 400s are sentences for an OPERATOR, not gateway faults.

    MEASURED on the box 2026-09-04: a dongle whose USB descriptors had stopped answering
    made rtl_power exit on `No matching devices found`, and the one thing the owner could
    act on would have reached them as a 502 reading "sdr sidecar: ..." — a status that
    says the box is broken, over a message that says which radio to reseat."""

    class _Posting:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, _path: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(
                400,
                json={"detail": "the radio did not start: No matching devices found."},
                request=httpx.Request("POST", "http://sdr/listen/start"),
            )

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", lambda **_kw: _Posting())

    with pytest.raises(HTTPException) as raised:
        await sdr_api._post(_settings(), "/listen/start", {})

    assert raised.value.status_code == 400
    assert "No matching devices found" in str(raised.value.detail)
    assert "sdr sidecar" not in str(raised.value.detail)


async def test_a_slow_reset_is_never_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED 2026-09-04, and the reason this is a test rather than a comment: a
    `USBDEVFS_RESET` outran the timeout and the owner got a 500 with a traceback for an
    operation that had in fact HAPPENED — the device left the bus. Answering "the radio
    did not reset" would have been worse than the traceback, because it is false. A
    timeout licenses "look again" and nothing more."""

    class _Slow:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, _path: str, **_kw: Any) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", lambda **_kw: _Slow())

    with pytest.raises(HTTPException) as raised:
        await sdr_api._post(_settings(), "/reset", {})

    assert raised.value.status_code == 504
    said = str(raised.value.detail)
    assert "may still be happening" in said
    # The words that would make it a lie.
    assert "did not reset" not in said
    assert "failed" not in said.lower()


async def test_a_reset_gets_longer_than_an_ordinary_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kernel waits on a port that may never answer, and that wait IS the operation.
    seen: list[float] = []

    class _Timed:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, _path: str, **_kw: Any) -> httpx.Response:
            return httpx.Response(
                200, json={"reset": True}, request=httpx.Request("POST", "http://sdr/reset")
            )

    def client(**kw: Any) -> Any:
        seen.append(kw["timeout"])
        return _Timed()

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", client)

    await sdr_api._post(_settings(), "/listen/start", {})
    await sdr_api._post(_settings(), "/reset", {}, wait_s=sdr_api.RESET_TIMEOUT_S)

    assert seen[1] > seen[0]


async def test_a_busy_radio_reaches_the_owner_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that just ends is a waterfall that never paints, with nothing on screen
    to say why. The sidecar's own words are what names the job holding the radio."""
    refusal = json.dumps({"detail": "the radio is logging APRS, not watching the spectrum"})
    _upstream(monkeypatch, _Resp(409, body=refusal.encode()))

    out = await sdr_api.spectrum(_request(), _settings(), OWNER)

    assert await _collect(out) == [
        'data: {"error": "the radio is logging APRS, not watching the spectrum"}\n\n'
    ]


async def test_a_viewer_who_left_stops_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise a closed tab keeps a radio's rows flowing through this process for the
    # life of the session.
    row = json.dumps({"start_hz": 88_000_000, "bin_hz": 25_000, "db": [-70.0]})
    _upstream(monkeypatch, _Resp(200, [row, row]))

    out = await sdr_api.spectrum(_request(disconnected=True), _settings(), OWNER)

    assert await _collect(out) == []
