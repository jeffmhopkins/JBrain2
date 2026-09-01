"""The debug SDR capture route: the S0b-ii gate, driven with no terminal.

What matters here is not the happy path — it is that the route reports honestly when
the radio produced nothing, when another caller holds it, and when there is no radio
at all. A capture of a dead antenna and a capture of a silent-but-working one have the
same duration, so `peak`/`heard_something` is the signal that tells them apart.
"""

import json
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from jbrain.api import debug


class _Settings:
    def __init__(self, url: str = "http://sdr:8000") -> None:
        self.sdr_url = url
        self.whisper_model = "whisper"


def _request(app_state: dict[str, object] | None = None) -> Request:
    app = FastAPI()
    for key, value in (app_state or {}).items():
        setattr(app.state, key, value)
    return Request({"type": "http", "app": app, "headers": [], "method": "POST", "path": "/"})


async def _capture(
    *, url: str = "http://sdr:8000", state: dict[str, object] | None = None, **kwargs: Any
) -> debug.SdrCaptureOut:
    """Call the route with a fake Settings and principal.

    The casts live here rather than at every call site: the route takes the real
    dependency types, and a test double is exactly what those annotations are meant
    to exclude in production code."""
    return await debug.sdr_capture(
        _request(state), cast(Any, _Settings(url)), cast(Any, None), **kwargs
    )


def _sidecar(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Point the route's httpx client at a fake sdr sidecar."""

    class FakeClient:
        def __init__(self, *_a, **_k) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a) -> None: ...
        async def post(self, path, json=None):  # noqa: A002 - httpx's parameter name
            return handler(path, json)

    monkeypatch.setattr(debug.httpx, "AsyncClient", FakeClient)


def _wav_response(meta: dict[str, object], body: bytes = b"RIFFfake") -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"X-Sdr-Meta": json.dumps(meta), "Content-Type": "audio/wav"},
        request=httpx.Request("POST", "http://sdr:8000/capture"),
    )


async def test_a_live_capture_reports_the_level_and_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sidecar(
        monkeypatch,
        lambda _p, _b: _wav_response(
            {"mode": "wbfm", "seconds": 8.0, "peak": 0.62, "device_log": "Tuned to 99.3 MHz"}
        ),
    )

    async def fake_transcribe(*_a, **_k):
        return {"text": "you're listening to the Space Coast"}

    monkeypatch.setattr(debug, "transcribe_audio_chunked", fake_transcribe)

    out = await _capture(
        state={"transcribe": object(), "local_gateway": None},
        frequency_mhz=99.3,
        mode="wbfm",
    )

    assert out.frequency_hz == 99_300_000
    assert out.heard_something is True
    assert out.peak == 0.62
    assert out.transcript == "you're listening to the Space Coast"
    assert out.transcript_error is None


async def test_a_silent_capture_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    # The failure this exists to catch: audio of the right LENGTH but no signal in it.
    # Whisper will happily invent words over noise, so the level is the honest read.
    _sidecar(
        monkeypatch,
        lambda _p, _b: _wav_response({"mode": "fm", "seconds": 8.0, "peak": 0.0008}),
    )

    async def fake_transcribe(*_a, **_k):
        return {"text": "thank you for watching"}  # a classic whisper-on-noise artefact

    monkeypatch.setattr(debug, "transcribe_audio_chunked", fake_transcribe)

    out = await _capture(state={"transcribe": object(), "local_gateway": None}, frequency_mhz=99.3)

    assert out.heard_something is False
    assert out.transcript == "thank you for watching"  # reported, but not endorsed


async def test_a_busy_radio_is_a_409_not_a_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    # One tuner: telling the caller plainly beats queueing them behind an unknown wait.
    _sidecar(
        monkeypatch,
        lambda _p, _b: httpx.Response(
            409, json={"detail": "busy"}, request=httpx.Request("POST", "http://sdr:8000/capture")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await _capture(frequency_mhz=99.3)

    assert raised.value.status_code == 409


async def test_no_radio_configured_is_a_clean_503() -> None:
    with pytest.raises(HTTPException) as raised:
        await _capture(url="", frequency_mhz=99.3)

    assert raised.value.status_code == 503
    assert "No SDR" in raised.value.detail


async def test_a_sidecar_error_surfaces_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _sidecar(
        monkeypatch,
        lambda _p, _b: httpx.Response(
            400,
            json={"detail": "rtl_fm produced no audio: No supported devices found."},
            request=httpx.Request("POST", "http://sdr:8000/capture"),
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await _capture(frequency_mhz=99.3)

    assert raised.value.status_code == 502
    assert "No supported devices" in raised.value.detail


async def test_a_transcription_failure_never_loses_the_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The audio is the expensive part; a whisper problem must not discard it.
    _sidecar(monkeypatch, lambda _p, _b: _wav_response({"mode": "fm", "seconds": 8.0, "peak": 0.4}))

    async def boom(*_a, **_k):
        raise RuntimeError("model not loaded")

    monkeypatch.setattr(debug, "transcribe_audio_chunked", boom)

    out = await _capture(state={"transcribe": object(), "local_gateway": None}, frequency_mhz=99.3)

    assert out.heard_something is True
    assert out.transcript is None
    assert "model not loaded" in (out.transcript_error or "")


async def test_the_frequency_reaching_the_sidecar_is_hz_never_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The plan's §4.4 invariant: the SDR lane carries a NUMBER, so the stream.py SSRF
    # guard is neither used nor widened.
    seen: dict[str, object] = {}

    def handler(path, body):
        seen["path"] = path
        seen["body"] = body
        return _wav_response({"mode": "fm", "seconds": 4.0, "peak": 0.3})

    _sidecar(monkeypatch, handler)

    await _capture(frequency_mhz=162.55, seconds=4.0, transcribe=False)

    assert seen["path"] == "/capture"
    body = cast(dict[str, Any], seen["body"])
    assert body["frequency_hz"] == 162_550_000
    assert not any(isinstance(v, str) and "://" in v for v in body.values() if v)
