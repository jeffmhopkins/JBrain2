"""Whether the official GrowBot repo has new body policies (`jbrain.growbot.upstream`).

`GET /growbot/upstream` answers from the last check; `POST /growbot/upstream/check` runs one
now (at most once a minute). The owner reads it on the Body tab; the debug token reads it
too, because the owner runs the box with no terminal (CLAUDE.md #10).
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request

from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.growbot.upstream import UpstreamStatus, UpstreamWatch

router = APIRouter(prefix="/growbot/upstream", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot/upstream")


def _watch(request: Request) -> UpstreamWatch:
    watch = getattr(request.app.state, "growbot_upstream", None)
    if watch is None:
        raise HTTPException(503, "the upstream check is not running")
    return cast(UpstreamWatch, watch)


@router.get("", response_model=UpstreamStatus)
async def upstream(request: Request, _: OwnerDep) -> UpstreamStatus:
    return await _watch(request).last()


@router.post("/check", response_model=UpstreamStatus)
async def check_upstream(request: Request, _: OwnerDep) -> UpstreamStatus:
    return await _watch(request).check(force=True)


@debug_router.get("", response_model=UpstreamStatus)
async def debug_upstream(request: Request, _: DebugDep) -> UpstreamStatus:
    return await _watch(request).last()


@debug_router.post("/check", response_model=UpstreamStatus)
async def debug_check_upstream(request: Request, _: DebugDep) -> UpstreamStatus:
    return await _watch(request).check(force=True)
