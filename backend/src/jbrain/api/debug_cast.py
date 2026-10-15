"""`/api/debug/cast/*` — the jpanel Cast spike's window onto the house's Chromecast TVs.

docs/mocks/jpanel-cast/README.md. Before any notice is cast, the plan needs to know what
each real TV reports when it is idle, off, casting, and running an app opened with the
remote — so this surface is read-mostly: the TVs and their live verdicts, each TV's
timeline, and one write, a test card, which the sidecar refuses unless the TV is idle or
off. Thin proxies over the host-networked `cast` sidecar (deploy/cast/server.py), which
takes `cast.token` from the internal secrets.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from jbrain.api import internal_auth
from jbrain.api.deps import DebugDep, SettingsDep
from jbrain.storage import InternalSecrets

router = APIRouter(prefix="/debug/cast")

SIDECAR_TIMEOUT_S = 15.0
# Tests swap in an httpx.MockTransport; None is the real network.
_transport: httpx.AsyncBaseTransport | None = None

TvId = Annotated[str, Path(pattern=r"^[0-9a-f-]{36}$")]


async def _call(
    request: Request,
    settings: Any,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = str(settings.cast_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="No cast sidecar on this box.")
    secrets = cast(InternalSecrets, request.app.state.internal_secrets)
    token = secrets.read(internal_auth.CAST)
    if not token:
        raise HTTPException(status_code=503, detail="cast.token is not minted yet")
    try:
        async with httpx.AsyncClient(
            base_url=base,
            timeout=SIDECAR_TIMEOUT_S,
            transport=_transport,
            headers={"Authorization": f"Bearer {token.decode()}"},
        ) as client:
            resp = await client.request(method, path, json=json, params=params)
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"cast sidecar unreachable ({type(exc).__name__}) — is it running?",
        ) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:300]}
    if resp.status_code == 401:
        raise HTTPException(status_code=502, detail="cast sidecar refused the api's token")
    if resp.status_code in (400, 404, 409):
        # A gate refusal carries the verdict that refused it; pass it through whole.
        raise HTTPException(status_code=resp.status_code, detail=body)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=body)
    return cast(dict[str, Any], body)


@router.get("")
async def cast_tvs(request: Request, settings: SettingsDep, _p: DebugDep) -> dict[str, Any]:
    """Every TV the box has found, each with its verdict (idle / standby / busy / unknown),
    why, and the raw reading the verdict came from."""
    request.state.debug_detail = "cast tvs"
    return await _call(request, settings, "GET", "/tvs")


@router.get("/{tv_id}/history")
async def cast_history(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    tv_id: TvId,
    since: Annotated[float, Query(ge=0)] = 0.0,
) -> dict[str, Any]:
    """One TV's timeline: each distinct reading with its verdict, and the events (found,
    connection, test cast / quit / refused) between them. `since` is a unix time."""
    request.state.debug_detail = f"cast history {tv_id}"
    return await _call(request, settings, "GET", f"/tvs/{tv_id}/history", params={"since": since})


class CastTestIn(BaseModel):
    seconds: int = Field(default=15, ge=5, le=120)


@router.post("/{tv_id}/test", status_code=202)
async def cast_test(
    request: Request, settings: SettingsDep, _p: DebugDep, tv_id: TvId, body: CastTestIn
) -> dict[str, Any]:
    """Cast the test card for `seconds`, then quit if it is still showing. 409 with the
    verdict when the TV is not idle or off: the gate is the sidecar's, checked at the
    instant of casting."""
    request.state.debug_detail = f"cast test {tv_id}"
    return await _call(
        request, settings, "POST", f"/tvs/{tv_id}/test", json={"seconds": body.seconds}
    )
