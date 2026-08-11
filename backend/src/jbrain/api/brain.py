"""Authenticated proxy from the PWA to the on-box tts-stt service's Kokoro renderer
(deploy/tts-stt: GET /tts, GET /tts/voices, GET /tts/health).

The tts-stt service is UNauthenticated and LAN-only, so the PWA (which may be off
the LAN entirely) can't reach it directly — but the api can, over the internal docker
network (the same link brainevents.py already POSTs display markers on). This router
lets the read-aloud voice picker and the read-aloud audio ride the owner's authenticated
api session instead: it lists the installed voices, reports the phonemizer health, and
renders a clip on demand.

It is on-box only (api -> tts-stt), never an egress under invariant #9. The text it
forwards is the answer the OWNER asked to be read: Kokoro renders it to audio and the api
returns the audio; nothing is stored, and — unlike the wall's opt-in llm-stream — nothing
is shown on the unauthenticated display, so there is no new exposure surface.
"""

from __future__ import annotations

import re

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for

log = structlog.get_logger()

router = APIRouter()

# The longest text we forward to a single render — mirrors the tts-stt service's own
# TTS_CHUNK_CAP; the PWA splits a reply into sentence-sized clips before calling here.
_TTS_TEXT_CAP = 1000


def _apply_pronunciation(text: str, lexicon: dict[str, str]) -> str:
    """Whole-word, case-insensitive respelling from the owner's lexicon ("Titusville" -> "Tight us
    ville"). Plain text, so it fixes a word on Kokoro's misaki OR espeak path. No-op when empty."""
    if not lexicon:
        return text
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in lexicon) + r")\b", re.IGNORECASE)
    lower = {w.lower(): s for w, s in lexicon.items()}
    return pattern.sub(lambda m: lower.get(m.group(0).lower(), m.group(0)), text)


def _brain_base(request: Request) -> str:
    """The `tts-stt` Kokoro TTS base URL for this app, or "" when it isn't configured (the
    proxy then 503s). Set at startup from JBRAIN_BRAIN_TTS_URL, so read-aloud + the voice
    picker reach the on-box renderer without touching an unauthenticated service directly."""
    base = getattr(request.app.state, "brain_tts_base_url", "")
    return base if isinstance(base, str) else ""


@router.get("/brain/voices")
async def brain_voices(principal: PrincipalDep, request: Request) -> JSONResponse:
    """The installed Kokoro voice ids ("kokoro-<voice>"), proxied from the tts-stt service's
    GET /tts/voices as `{"voices": [...]}`. 503 when the service is unconfigured or unreachable
    so the picker can fall back to "no voices" (and read-aloud to the device voice)."""
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


@router.get("/brain/tts/health")
async def brain_tts_health(principal: PrincipalDep, request: Request) -> JSONResponse:
    """Which phonemizer the on-box Kokoro is ACTUALLY using, proxied from the tts-stt service's
    GET /tts/health as `{"kokoro_available": bool, "g2p": "misaki"|"espeak"|"unavailable", ...}`.
    `g2p == "espeak"` means Kokoro fell back off misaki (a silent, stderr-only degrade), so the
    KOKORO_LEXICON pronunciation overrides are inert and seeded words like "Titusville"
    mispronounce again — the Settings panel shows this instead of it needing an SSH log grep. The
    first call pre-warms misaki (a one-time multi-second spaCy load), hence the generous timeout.
    503 when the service is unconfigured/unreachable so the panel can show "unknown"."""
    base = _brain_base(request)
    if not base:
        raise HTTPException(status_code=503, detail="tts service not configured")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{base}/tts/health")
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
    speed: float | None = None,
    trail: int | None = None,
) -> Response:
    """Render `text` to a WAV in `voice` (a voice id from /brain/voices) via the on-box
    Kokoro and return the audio. The PWA read-aloud and the Settings "play sample" button
    both call this. Text is bounded; `lead` (silence pad, ms) and the audiobook-pacing controls
    `speed`/`trail` (ms) are clamped and passed through so a multi-clip reply plays gaplessly and
    the reading style (set by the PWA's markup-vs-prose classifier) reaches the box."""
    base = _brain_base(request)
    if not base:
        raise HTTPException(status_code=503, detail="tts service not configured")
    clipped = text[:_TTS_TEXT_CAP]
    if not clipped.strip():
        raise HTTPException(status_code=400, detail="no text")
    # Apply the owner's respelling lexicon here (the api holds the principal + DB; the box is
    # DB-free). A plain whole-word text substitution — engine-agnostic, so a fixed word is right on
    # the misaki OR espeak path — done before forwarding so the box renders the respelled text.
    # Best-effort: a settings-read hiccup must never fail read-aloud, only skip the respelling.
    store = getattr(request.app.state, "settings_store", None)
    if store is not None:
        try:
            lexicon = await store.pronunciation_lexicon(ctx_for(principal))
            clipped = _apply_pronunciation(clipped, lexicon)
        except Exception as exc:  # noqa: BLE001 — respelling is a nicety, never a render blocker
            log.warning("brain_tts.lexicon_unavailable", error=str(exc))
    params: dict[str, str] = {"text": clipped, "voice": voice}
    if lead is not None:
        params["lead"] = str(max(0, min(2000, lead)))
    if speed is not None:
        params["speed"] = str(max(0.5, min(2.0, speed)))
    if trail is not None:
        params["trail"] = str(max(0, min(3000, trail)))
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
