"""The GrowBot body test's routes (`jbrain.growbot.bodytests`).

- `internal_router` — the sidecar (`growbot.token`): `POST /internal/growbot/bodytests` keeps one
  chunk of a run's log, as the creature's phone posted it to `/api/box/bodytest`.
- `router` — the owner (`OwnerDep`): `GET /api/growbot/bodytests?robot=` the newest runs (the
  Body tab's live mirror and its list of past runs), `GET /api/growbot/bodytests/{n}` one run
  with its whole log, as JSON to download.
- `debug_router` — a debug token: the same two reads under `/api/debug/growbot/bodytests`, so a
  run can be reviewed from a debug session (owner, 2026-11-15: "log everything so you can review
  after").
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep
from jbrain.api.internal_auth import require_growbot
from jbrain.db.session import SessionContext
from jbrain.growbot.bodytests import ROWS_MAX, SEQ_MAX, BodyTests, Chunk, Run

internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])
router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_ROBOT = r"^gb-[A-Za-z0-9]{1,32}$"


class ChunkIn(BaseModel):
    run: str = Field(pattern=r"^bt-[a-z0-9]{1,16}-[a-z0-9]{1,16}$")
    robot: str = Field(pattern=_ROBOT)
    persona: str | None = Field(default=None, pattern=_UUID)
    seq: int = Field(ge=0, lt=SEQ_MAX)
    status: Literal["running", "done", "stopped", "failed"] = "running"
    summary: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=ROWS_MAX)
    skills: dict[str, str] = Field(default_factory=dict, max_length=3)


class KeptOut(BaseModel):
    ok: bool
    number: int | None = None


class RunOut(BaseModel):
    number: int
    run: str
    robot: str
    started_at: datetime
    updated_at: datetime
    status: str
    skills: dict[str, Any]
    summary: dict[str, Any]
    rows: int


class RunLogOut(RunOut):
    log: list[dict[str, Any]]


def _store(request: Request) -> BodyTests:
    store = getattr(request.app.state, "growbot_bodytests", None)
    if store is None:
        raise HTTPException(503, "Body tests aren't kept on this box.")
    return cast(BodyTests, store)


def _out(r: Run) -> RunOut:
    return RunOut(
        number=r.id,
        run=r.run,
        robot=r.robot,
        started_at=r.started_at,
        updated_at=r.updated_at,
        status=r.status,
        skills=r.skills,
        summary=r.summary,
        rows=r.rows,
    )


@internal_router.post("/bodytests")
async def record_chunk(request: Request, body: ChunkIn) -> KeptOut:
    number = await _store(request).record(
        _SYSTEM_OWNER,
        Chunk(
            run=body.run,
            robot=body.robot,
            seq=body.seq,
            status=body.status,
            summary=body.summary,
            rows=body.rows,
            persona_id=body.persona,
            skills=body.skills,
        ),
    )
    return KeptOut(ok=number is not None, number=number)


async def _runs(request: Request, ctx: SessionContext, robot: str, limit: int) -> list[RunOut]:
    return [_out(r) for r in await _store(request).runs(ctx, robot=robot, limit=limit)]


async def _one(request: Request, ctx: SessionContext, number: int) -> RunLogOut:
    store = _store(request)
    run = await store.run(ctx, number)
    if run is None:
        raise HTTPException(404, "no such body test")
    return RunLogOut(**_out(run).model_dump(), log=await store.rows(ctx, number))


Robot = Annotated[str, Query(pattern=r"^(gb-[A-Za-z0-9]{1,32})?$")]
Limit = Annotated[int, Query(ge=1, le=50)]


@router.get("/bodytests")
async def list_runs(
    request: Request, owner: OwnerDep, robot: Robot = "", limit: Limit = 10
) -> list[RunOut]:
    ctx = SessionContext(principal_id=owner.id, principal_kind="owner")
    return await _runs(request, ctx, robot, limit)


@router.get("/bodytests/{number}")
async def get_run(request: Request, owner: OwnerDep, number: int) -> RunLogOut:
    ctx = SessionContext(principal_id=owner.id, principal_kind="owner")
    return await _one(request, ctx, number)


@debug_router.get("/bodytests")
async def debug_list_runs(
    request: Request, _p: DebugDep, robot: Robot = "", limit: Limit = 10
) -> list[RunOut]:
    return await _runs(request, _DEBUG_OWNER, robot, limit)


@debug_router.get("/bodytests/{number}")
async def debug_get_run(request: Request, _p: DebugDep, number: int) -> RunLogOut:
    return await _one(request, _DEBUG_OWNER, number)
