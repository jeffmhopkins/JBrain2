"""`/api/debug/cast/*` — the jpanel Cast spike's window onto the house's Chromecast TVs.

docs/mocks/jpanel-cast/README.md. Before any notice is cast, the plan needs to know what
each real TV reports when it is idle, off, casting, and running an app opened with the
remote — so this surface is read-mostly: the TVs and their live verdicts, each TV's
timeline, and one write, a test card, which the sidecar refuses unless the TV is idle or
off. Thin proxies over the host-networked `cast` sidecar (deploy/cast/server.py), which
takes `cast.token` from the internal secrets.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, SettingsDep
from jbrain.cast import sidecar

router = APIRouter(prefix="/debug/cast")

TvId = Annotated[str, Path(pattern=r"^[0-9a-f-]{36}$")]


async def _call(
    request: Request, settings: Any, method: str, path: str, **kw: Any
) -> dict[str, Any]:
    return await sidecar.call(request, settings, method, path, **kw)


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
