"""jerv's radio tools.

`sdr_listen` is load-bearing beyond chat: the composer's radio icon exists only
while a session holds the tuner, so this tool is the only thing that can put the
tuner surface in front of the owner. What matters is that it takes the lease, says
so plainly when it cannot, and points at the icon rather than narrating settings.
"""

from typing import Any

import httpx
import pytest

from jbrain.agent.sdrtools import build_sdr_handlers


def _client(handler) -> Any:
    class Fake:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a: Any) -> None: ...
        async def post(self, path: str, json: dict[str, Any] | None = None):
            return handler(path, json or {})

    return Fake


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    def install(handler) -> dict:
        monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
        return build_sdr_handlers("http://sdr:8000")

    return install


def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body, request=httpx.Request("POST", "http://sdr:8000/x"))


async def test_listening_points_at_the_icon_rather_than_narrating(tools) -> None:
    seen: dict[str, Any] = {}

    def handler(path, body):
        seen.update({"path": path, "body": body})
        return _ok({"session_id": "abc", "frequency_hz": 99_300_000, "mode": "wbfm"})

    out = await tools(handler)["sdr_listen"]({"frequency_mhz": 99.3}, None)

    assert seen["body"]["frequency_hz"] == 99_300_000
    # The owner can drive the tuner far faster than the model can describe it.
    assert "icon" in out
    assert "99.3" in out


async def test_broadcast_fm_defaults_to_wide_fm(tools) -> None:
    # Getting this wrong is audible: narrowband on a broadcast station is mush.
    seen: dict[str, Any] = {}
    out = await tools(lambda p, b: (seen.update(b), _ok({"session_id": "abc"}))[1])["sdr_listen"](
        {"frequency_mhz": 99.3}, None
    )

    assert seen["mode"] == "wbfm"
    assert "WBFM" in out


async def test_everything_else_defaults_to_narrowband(tools) -> None:
    seen: dict[str, Any] = {}
    await tools(lambda p, b: (seen.update(b), _ok({"session_id": "abc"}))[1])["sdr_listen"](
        {"frequency_mhz": 162.55}, None
    )

    assert seen["mode"] == "fm"


async def test_an_explicit_mode_wins_over_the_default(tools) -> None:
    seen: dict[str, Any] = {}
    await tools(lambda p, b: (seen.update(b), _ok({"session_id": "abc"}))[1])["sdr_listen"](
        {"frequency_mhz": 99.3, "mode": "am"}, None
    )

    assert seen["mode"] == "am"


async def test_a_busy_radio_is_explained_not_retried(tools) -> None:
    busy = httpx.Response(
        409, json={"detail": "busy"}, request=httpx.Request("POST", "http://sdr:8000/x")
    )

    out = await tools(lambda _p, _b: busy)["sdr_listen"]({"frequency_mhz": 99.3}, None)

    # One tuner: this is a state to report, not an error to retry into.
    assert "already listening" in out
    assert "release" in out.lower()


async def test_an_out_of_range_frequency_never_reaches_the_radio(tools) -> None:
    called = False

    def handler(_p, _b):
        nonlocal called
        called = True
        return _ok({})

    out = await tools(handler)["sdr_listen"]({"frequency_mhz": 5000}, None)

    assert not called
    assert "outside what this radio can tune" in out


async def test_a_non_numeric_frequency_is_refused_kindly(tools) -> None:
    out = await tools(lambda _p, _b: _ok({}))["sdr_listen"]({"frequency_mhz": "ninety nine"}, None)

    assert "isn't a number" in out


async def test_stop_releases_and_says_so(tools) -> None:
    out = await tools(lambda _p, _b: _ok({"stopped": True}))["sdr_stop"]({}, None)

    assert out == "Radio released."


async def test_stopping_an_idle_radio_is_not_an_error(tools) -> None:
    out = await tools(lambda _p, _b: _ok({"stopped": False}))["sdr_stop"]({}, None)

    assert "wasn't listening" in out


def test_a_box_with_no_radio_gets_no_tools() -> None:
    # The same graceful degrade the image and transcription tools use — and it means
    # the tuner surface can never appear on a box that has no radio.
    assert build_sdr_handlers("") == {}
