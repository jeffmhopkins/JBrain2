"""The owner-only radio control API — what the omnibox tuner drives.

`GET /sdr/status` is the one the composer polls: its `listening` field is what lights
the radio icon, so "a session is running" and "the owner sees a radio icon" are the
same fact rather than two that can drift apart. Start/tune/stop move the radio;
`GET /sdr/audio` proxies the sidecar's live Opus so the browser fetches it
same-origin (the `radio` network is internal — the PWA has no route to the sidecar).

Every route is `OwnerDep`. The radio is a physical device on the owner's box: there
is no scoped-token or family case for it, so the surface is simply unreachable by
anything but the owner.

**No URL ever crosses this boundary.** Routes take a frequency and a mode, bounded
here as well as in the sidecar, and the sidecar's base URL is pinned in settings.
That is what keeps `stream.py`'s SSRF guard untouched rather than widened
(docs/plans/SDR_RADIO_PLAN.md §4.4).

This module touches no LLM (rule 1 n/a), no storage, and no database — the radio is
process state in the sidecar, not a row — so there is no RLS surface to scope. The
recordings library, which does have one, is a later wave.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jbrain.api.deps import OwnerDep, SettingsDep
from jbrain.transcribe import WhisperCppClient

router = APIRouter(prefix="/sdr", tags=["sdr"])

# The R820T2's real range, and the demodulators rtl_fm offers. Bounded here as well
# as in the sidecar because a bound that lives only in the caller is not a bound.
MIN_MHZ = 0.024
MAX_MHZ = 1766.0
MODES = ("fm", "nfm", "wbfm", "am", "usb", "lsb")

# Long enough for a slow tuner open, short enough that a wedged sidecar surfaces as
# an error rather than a hung composer.
_TIMEOUT = 30.0


class SdrStatusOut(BaseModel):
    """`listening` is None when the radio is idle — which is exactly the condition
    the omnibox uses to decide whether the icon exists at all."""

    available: bool
    listening: dict[str, Any] | None


def _base(settings: Any) -> str:
    url = cast(str, settings.sdr_url or "")
    if not url:
        raise HTTPException(status_code=503, detail="No SDR on this box.")
    return url


async def _post(settings: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=_base(settings), timeout=_TIMEOUT) as client:
        resp = await client.post(path, json=body)
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=_detail(resp, "The radio is busy."))
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"sdr sidecar: {_detail(resp, resp.text[:300])}"
        )
    return cast(dict[str, Any], resp.json())


def _detail(resp: httpx.Response, fallback: str) -> str:
    try:
        return cast(str, resp.json().get("detail") or fallback)
    except ValueError:
        return fallback


@router.get("/status")
async def status(settings: SettingsDep, _owner: OwnerDep) -> SdrStatusOut:
    """What the radio is doing. Answers `available: false` on a box with no radio
    rather than erroring, so the composer can simply never show the icon."""
    if not settings.sdr_url:
        return SdrStatusOut(available=False, listening=None)
    try:
        async with httpx.AsyncClient(base_url=settings.sdr_url, timeout=5.0) as client:
            resp = await client.get("/healthz")
        health = cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError):
        # The sidecar is configured but unreachable (starting, crashed). Idle is the
        # honest answer — the icon stays dark rather than lit over a dead radio.
        return SdrStatusOut(available=False, listening=None)
    return SdrStatusOut(available=True, listening=health.get("listening"))


@router.post("/listen")
async def listen(
    settings: SettingsDep,
    _owner: OwnerDep,
    frequency_mhz: Annotated[float, Query(gt=MIN_MHZ, lt=MAX_MHZ)],
    mode: Annotated[str, Query(pattern=f"^({'|'.join(MODES)})$")] = "wbfm",
    gain: Annotated[str | None, Query(max_length=16)] = None,
) -> dict[str, Any]:
    """Take the radio and start listening. 409 when it is already held."""
    return await _post(
        settings,
        "/listen/start",
        {"frequency_hz": int(round(frequency_mhz * 1_000_000)), "mode": mode, "gain": gain},
    )


@router.post("/tune")
async def tune(
    settings: SettingsDep,
    _owner: OwnerDep,
    frequency_mhz: Annotated[float, Query(gt=MIN_MHZ, lt=MAX_MHZ)],
    mode: Annotated[str | None, Query(pattern=f"^({'|'.join(MODES)})$")] = None,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Retune the live session. The session id survives, so the icon does not blink."""
    body: dict[str, Any] = {"frequency_hz": int(round(frequency_mhz * 1_000_000))}
    if mode is not None:
        body["mode"] = mode
    if session_id is not None:
        body["session_id"] = session_id
    return await _post(settings, "/listen/tune", body)


@router.post("/stop")
async def stop(
    settings: SettingsDep,
    _owner: OwnerDep,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Release the radio. This is what makes the omnibox icon disappear."""
    return await _post(settings, "/listen/stop", {"session_id": session_id})


@router.get("/audio")
async def audio(request: Request, settings: SettingsDep, _owner: OwnerDep) -> StreamingResponse:
    """Proxy the sidecar's live Opus to the browser.

    A proxy rather than a redirect because the `radio` network is internal: the PWA
    cannot reach the sidecar directly, and it should not be able to. Streaming it
    through here also means the audio inherits the owner session — an unauthenticated
    URL for the box's live radio is not something to hand out."""
    base = _base(settings)

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        try:
            async with client.stream("GET", "/listen/audio") as upstream:
                if upstream.status_code != 200:
                    return
                async for chunk in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(pump(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.get("/captions")
async def captions(request: Request, settings: SettingsDep, _owner: OwnerDep) -> StreamingResponse:
    """Live captions for whatever the radio is playing, as server-sent events.

    Whisper is not a streaming model — it transcribes a finished clip — so the sidecar
    cuts the live audio into segments on the quiet gaps between transmissions, and each
    one is transcribed and emitted as it lands. Words carry the per-token confidence the
    client already asks for, so the caption tints on the same rose-amber-green scale as
    the transcript viewer.

    **The model is deliberately NOT freed between segments.** Measured on this box, a
    transcription costs ~10.7 s whether the clip is 4 s or 11 s: almost all of it is
    loading and unloading the model, and 7 extra seconds of audio added 0.04 s. The
    capture route pays that every call because it unloads when it finishes; a captioner
    that did the same would fall permanently behind. Holding the model resident is what
    makes this affordable — and it is also why captions are an explicit toggle rather
    than always-on, since a resident whisper shares the GPU with the chat model.

    Segments arrive already squelched: the sidecar drops any whose loudest moment never
    crossed the floor, because whisper answers an empty band with fluent invented
    sentences rather than silence."""
    base = _base(settings)
    if not settings.whisper_url:
        raise HTTPException(
            status_code=503,
            detail="no whisper gateway on this box, so live captions cannot run",
        )
    whisper = WhisperCppClient(
        settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
    )

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        try:
            async with client.stream("GET", "/listen/segments") as upstream:
                if upstream.status_code != 200:
                    yield _event({"error": "the radio is not listening"})
                    return
                async for started, wav in _segments(upstream):
                    if await request.is_disconnected():
                        break
                    if wav is None:  # a keep-alive, so a quiet channel holds the socket
                        yield ": keepalive\n\n"
                        continue
                    try:
                        result = await whisper.transcribe(
                            wav, filename="segment.wav", media_type="audio/wav"
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        # One bad segment must not end the caption stream; the next
                        # transmission is a fresh chance and the owner keeps listening.
                        yield _event({"error": str(exc)[:200], "started_at": started})
                        continue
                    text = result.text.strip()
                    if not text:
                        continue
                    yield _event(
                        {
                            "started_at": started,
                            "text": text,
                            "words": [
                                {"text": w.text, "confidence": round(w.confidence, 4)}
                                for w in result.words
                            ],
                        }
                    )
        finally:
            await client.aclose()

    return StreamingResponse(
        pump(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


def _event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _segments(upstream: httpx.Response):
    """Split the sidecar's newline-framed stream into (started_at, wav) pairs.

    The frame is a JSON header line then exactly `bytes` of WAV. Framing rather than a
    request per segment because the gap between requests always lands mid-sentence."""
    buffer = b""
    async for block in upstream.aiter_bytes():
        buffer += block
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            try:
                head = json.loads(buffer[:newline] or b"{}")
            except json.JSONDecodeError:
                buffer = buffer[newline + 1 :]
                continue
            if head.get("keepalive"):
                buffer = buffer[newline + 1 :]
                yield 0.0, None
                continue
            size = int(head.get("bytes", 0))
            if size <= 0:
                # Not a segment frame — a blank line, or a header that lost its size.
                # Yielding it would hand whisper zero bytes of audio to describe.
                buffer = buffer[newline + 1 :]
                continue
            if len(buffer) < newline + 1 + size:
                break  # the WAV has not all arrived yet
            wav = buffer[newline + 1 : newline + 1 + size]
            buffer = buffer[newline + 1 + size :]
            yield float(head.get("started_at", 0.0)), wav
