"""Authenticated proxy from the PWA to the on-box tts-stt service's piper renderer
(deploy/tts-stt: GET /tts, GET /tts/voices).

The tts-stt service is UNauthenticated and LAN-only, so the PWA (which may be off
the LAN entirely) can't reach it directly — but the api can, over the internal docker
network (the same link brainevents.py already POSTs display markers on). This router
lets the read-aloud voice picker and the read-aloud audio ride the owner's authenticated
api session instead: it lists the installed voices and renders a clip on demand.

It is on-box only (api -> tts-stt), never an egress under invariant #9. The text it
forwards is the answer the OWNER asked to be read: piper renders it to audio and the api
returns the audio; nothing is stored, and — unlike the wall's opt-in llm-stream — nothing
is shown on the unauthenticated display, so there is no new exposure surface.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from jbrain.api.deps import PrincipalDep

log = structlog.get_logger()

router = APIRouter()

# The longest text we forward to a single render — mirrors the tts-stt service's own
# TTS_CHUNK_CAP; the PWA splits a reply into sentence-sized clips before calling here.
_TTS_TEXT_CAP = 1000


def _brain_base(request: Request) -> str:
    """The `tts-stt` piper TTS base URL for this app, or "" when it isn't configured (the
    proxy then 503s). Set at startup from JBRAIN_BRAIN_TTS_URL, so read-aloud + the voice
    picker reach the on-box renderer without touching an unauthenticated service directly."""
    base = getattr(request.app.state, "brain_tts_base_url", "")
    return base if isinstance(base, str) else ""


@router.get("/brain/voices")
async def brain_voices(principal: PrincipalDep, request: Request) -> JSONResponse:
    """The installed piper voice ids (incl. curated multi-speaker entries), proxied from
    the tts-stt service's GET /tts/voices as `{"voices": [...]}`. 503 when the service is
    unconfigured or unreachable so the picker can fall back to "no voices"."""
    base = _brain_base(request)
    if not base:
        raise HTTPException(status_code=503, detail="tts service not configured")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/tts/voices")
        if resp.status_code != 200:
            raise HTTPException(status_code=503, detail="tts service unavailable")
        return JSONResponse(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="tts service unreachable") from exc


@router.get("/brain/tts")
async def brain_tts(
    principal: PrincipalDep,
    request: Request,
    text: str = "",
    voice: str = "",
    lead: int | None = None,
) -> Response:
    """Render `text` to a WAV in `voice` (a voice id from /brain/voices) via the on-box
    piper and return the audio. The PWA read-aloud and the Settings "play sample" button
    both call this. Text is bounded; `lead` (silence pad, ms) is clamped and passed through
    so a multi-clip reply plays gaplessly."""
    base = _brain_base(request)
    if not base:
        raise HTTPException(status_code=503, detail="tts service not configured")
    clipped = text[:_TTS_TEXT_CAP]
    if not clipped.strip():
        raise HTTPException(status_code=400, detail="no text")
    params: dict[str, str] = {"text": clipped, "voice": voice}
    if lead is not None:
        params["lead"] = str(max(0, min(2000, lead)))
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{base}/tts", params=params)
    except httpx.HTTPError as exc:
        # A silent failure here makes the PWA fall back to the device's native voice, so
        # log the reason — pair with the box's own `[tts]` line to place the failure.
        log.warning("brain_tts.unreachable", voice=voice, error=str(exc))
        raise HTTPException(status_code=503, detail="tts service unreachable") from exc
    if resp.status_code != 200:
        log.warning("brain_tts.upstream_failed", voice=voice, status=resp.status_code)
        raise HTTPException(status_code=502, detail="tts failed")
    return Response(
        content=resp.content,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )
