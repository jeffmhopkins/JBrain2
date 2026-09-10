"""`GET /api/sdr/status` — the one route the composer icon reads.

It had no test, and B7 gave it a decision to make: which of several live sessions the
owner SEES is now settled here (`health.shown`) rather than in the radio process. The
sidecar still sends a `listening` field, but it means something narrower now — the
tuner's session — so a test that lets the two agree proves nothing. Every case below
makes the sidecar's answer DIFFER from the right one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jbrain.api import sdr as sdr_api

OWNER = SimpleNamespace(id="owner", kind="owner")
WHIP, WIRE = "09022796", "77192819"


#: Captured once, at import. Read inside `_sidecar` it would pick up the PREVIOUS patch
#: on a second call in one test, and the two builders would fight over `transport` —
#: silently answering the second call with the first body.
_REAL_CLIENT = httpx.AsyncClient


def _sidecar(monkeypatch: pytest.MonkeyPatch, payload: Any, *, dead: bool = False) -> None:
    """Answer `/healthz` with exactly this body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if dead:
            raise httpx.ConnectError("no sidecar")
        return httpx.Response(200, json=payload)

    def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*args, **kwargs)

    monkeypatch.setattr(sdr_api.httpx, "AsyncClient", build)


def _request() -> Any:
    """A request whose app holds no recorder — a box that has never recorded.

    The route reads the recorder off `app.state` (it is api state, not something
    /healthz reports), so a request without an app is not a request this route ever
    sees."""
    return SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))


async def _status(monkeypatch: pytest.MonkeyPatch, payload: Any, **kw: Any) -> Any:
    _sidecar(monkeypatch, payload, **kw)
    settings = SimpleNamespace(sdr_url="http://sdr:8000")
    return await sdr_api.status(_request(), settings, OWNER)  # type: ignore[arg-type]


async def test_the_api_picks_the_icon_even_when_the_sidecar_named_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sidecar's `listening` is the TUNER's session; with nothing listening it names
    whatever it holds. Here that is the spectrum sweep, and APRS is what a person is
    actually asking about — so the two answers must differ, and ours must win."""
    aprs = {"purpose": "aprs", "session_id": "s-aprs", "serial": WIRE}
    sweep = {"purpose": "spectrum", "session_id": "s-sweep", "serial": WHIP}

    out = await _status(monkeypatch, {"listening": sweep, "sessions": [sweep, aprs]})

    assert out.available is True
    assert out.listening is not None and out.listening["session_id"] == "s-aprs"
    # ...and the full list is untouched: the APRS tab reads it to ask its own question.
    assert [s["session_id"] for s in out.sessions] == ["s-sweep", "s-aprs"]


async def test_an_older_sidecar_that_sends_no_sessions_still_lights_the_icon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The api and the sidecar are separate containers and an update restarts them one
    at a time, so for a few seconds one of them is the previous build. That build holds
    at most one session, and sends it only as `listening`."""
    one = {"purpose": "listen", "session_id": "s-old", "serial": WHIP}

    out = await _status(monkeypatch, {"listening": one})

    assert out.listening == one
    assert out.sessions == [one]


async def test_a_sidecar_holding_nothing_leaves_the_icon_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _status(monkeypatch, {"listening": None, "sessions": []})

    assert out.available is True
    assert out.listening is None


async def test_a_body_this_route_cannot_model_darkens_no_icon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 here darkens the composer icon over a radio that is working, so a row it
    cannot read is dropped rather than raised. `SdrStatusOut.sessions` is typed; without
    the check the string below reaches pydantic."""
    real = {"purpose": "listen", "session_id": "s-1", "serial": WHIP}

    out = await _status(monkeypatch, {"listening": "yes", "sessions": [real, "junk"]})

    assert out.listening == real
    assert out.sessions == [real]

    # ...and a `sessions` that is not a list at all falls back to `listening`, which here
    # is not a session either — so the honest answer is nothing running.
    out = await _status(monkeypatch, {"listening": "yes", "sessions": "nope"})
    assert out.listening is None
    assert out.sessions == []


async def test_an_unreachable_sidecar_reads_as_no_radio_rather_than_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting or crashed. Idle is the honest answer — the icon stays dark rather than
    lit over a dead radio, and the composer never sees a 500."""
    out = await _status(monkeypatch, {}, dead=True)

    assert out.available is False
    assert out.listening is None


async def test_a_box_with_no_radio_configured_never_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await sdr_api.status(_request(), SimpleNamespace(sdr_url=""), OWNER)  # type: ignore[arg-type]

    assert out.available is False


async def test_the_debug_twin_shows_exactly_what_the_icon_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GET /api/sdr/status` is `OwnerDep`, so a handed-over token could start a session
    and never read back what the owner's screen says about it (CLAUDE.md #10).

    The twin must not re-derive the answer: B7 exists to have ONE. Asserting they agree
    on a body where the sidecar's own `listening` is a third answer is what makes that
    load-bearing rather than incidental.
    """
    from jbrain.api import debug as debug_api

    aprs = {"purpose": "aprs", "session_id": "s-aprs", "serial": WIRE}
    sweep = {"purpose": "spectrum", "session_id": "s-sweep", "serial": WHIP}
    _sidecar(monkeypatch, {"listening": sweep, "sessions": [sweep, aprs]})
    settings = SimpleNamespace(sdr_url="http://sdr:8000")
    request = _request()

    twin = await debug_api.sdr_sessions_debug(request, settings, None)  # type: ignore[arg-type]
    owner = await sdr_api.status(_request(), settings, OWNER)  # type: ignore[arg-type]

    assert twin.listening is not None and twin.listening["session_id"] == "s-aprs"
    assert twin.model_dump() == owner.model_dump()
    # ...and the console's audit line says which route ran, like every other one.
    assert request.state.debug_detail == "sdr sessions"
