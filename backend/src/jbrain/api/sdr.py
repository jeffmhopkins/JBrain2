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

from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from jbrain.api.deps import OwnerDep, SettingsDep

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
