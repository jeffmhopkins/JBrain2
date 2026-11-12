"""The GrowBot brain log's routes (`jbrain.growbot.brainlog`, the diary in `.journal`).

- `internal_router` — the sidecar (`growbot.token`): `POST /internal/growbot/journal` keeps the
  rows of one diary snapshot the creature posted to `/api/journal`, each row once.
- `router` — the owner (`OwnerDep`): `POST /api/growbot/brain-log` downloads the zip for the
  last 1, 3 or 6 hours. A POST, so the names to hide never sit in a URL or an access log.
- `debug_router` — a debug token: `GET /api/debug/growbot/brain-log?hours=` the same zip,
  always redacted and with no names list, so a debug session can see what the owner would send.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep, SettingsDep
from jbrain.api.growbot_calls import summarize
from jbrain.api.internal_auth import require_growbot
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.growbot import brainlog
from jbrain.growbot.calls import CallLog
from jbrain.growbot.journal import ROWS_MAX, DiaryBatch, Journal
from jbrain.growbot.personas import PersonaRepo
from jbrain.growbot.repo import GrowbotRepo

log = structlog.get_logger()

internal_router = APIRouter(prefix="/growbot", dependencies=[Depends(require_growbot)])
router = APIRouter(prefix="/growbot", tags=["growbot"])
debug_router = APIRouter(prefix="/debug/growbot")

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

Hours = Literal[1, 3, 6]


class DiaryIn(BaseModel):
    # Upstream's diary id: "nx-" and up to 20 of [A-Za-z0-9_-] (next.html LOG_SID).
    sid: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    persona: str | None = Field(default=None, pattern=_UUID)
    robot: str = Field(default="", pattern=r"^(gb-[A-Za-z0-9]{1,32})?$")
    head: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=ROWS_MAX)


class KeptOut(BaseModel):
    ok: bool
    new: int


def _journal(request: Request) -> Journal | None:
    return cast(Journal | None, getattr(request.app.state, "growbot_journal", None))


@internal_router.post("/journal")
async def record_diary(request: Request, body: DiaryIn) -> KeptOut:
    journal = _journal(request)
    if journal is None:
        return KeptOut(ok=False, new=0)
    new = await journal.record(
        _SYSTEM_OWNER,
        DiaryBatch(
            sid=body.sid, rows=body.rows, persona_id=body.persona, robot=body.robot, head=body.head
        ),
    )
    return KeptOut(ok=True, new=new)


class BrainLogIn(BaseModel):
    hours: Hours = 1
    redact: bool = True
    names: list[Annotated[str, Field(max_length=60)]] = Field(
        default_factory=list, max_length=brainlog.NAMES_MAX
    )


async def _zip(
    request: Request,
    ctx: SessionContext,
    settings: Settings,
    *,
    hours: int,
    redact: bool,
    names: list[str],
) -> Response:
    state = request.app.state
    # One at a time: a six-hour zip is held in memory while it is built.
    lock = getattr(state, "growbot_brainlog_lock", None)
    if lock is None:
        lock = state.growbot_brainlog_lock = asyncio.Lock()
    if lock.locked():
        raise HTTPException(409, "A brain log is already being gathered. Try again in a moment.")
    async with lock:
        return await _build(request, ctx, settings, hours=hours, redact=redact, names=names)


async def _build(
    request: Request,
    ctx: SessionContext,
    settings: Settings,
    *,
    hours: int,
    redact: bool,
    names: list[str],
) -> Response:
    state = request.app.state
    persona_repo = cast(PersonaRepo, state.growbot_personas)
    personas = await persona_repo.list_personas(ctx)
    robots = await cast(GrowbotRepo, state.growbot_repo).list_robots(ctx)
    souls: list[dict[str, Any]] = []
    metas: list[dict[str, Any]] = []
    for p in personas:
        full = await persona_repo.get_persona(ctx, p.id)
        if full is not None and full.soul:
            souls.append(full.soul)
        metas += [m.meta for m in await persona_repo.memories(ctx, p.id) or []]
    # Persona and robot names may be people's too (a creature named after a child).
    known = brainlog.known_names(souls, metas) + [p.name for p in personas]
    known += [r.name for r in robots if r.name]
    data, filename = await brainlog.build(
        ctx=ctx,
        calls=cast(CallLog, state.growbot_calls),
        journal=_journal(request),
        personas=personas,
        robots=robots,
        summarize=summarize,
        hours=hours,
        redact=redact,
        names=names,
        now=datetime.now(UTC),
        jbrain=settings.git_sha,
        known=known,
    )
    log.info("growbot.brain_log", hours=hours, redact=redact, bytes=len(data))
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/brain-log")
async def brain_log(
    request: Request, owner: OwnerDep, settings: SettingsDep, body: BrainLogIn
) -> Response:
    ctx = SessionContext(principal_id=owner.id, principal_kind="owner")
    return await _zip(
        request, ctx, settings, hours=body.hours, redact=body.redact, names=body.names
    )


@debug_router.get("/brain-log")
async def debug_brain_log(
    request: Request,
    _p: DebugDep,
    settings: SettingsDep,
    hours: Annotated[int, Query()] = 1,
) -> Response:
    # A query string arrives as text, which a Literal of ints refuses outright.
    if hours not in brainlog.HOURS:
        raise HTTPException(422, "hours is 1, 3 or 6")
    return await _zip(request, _DEBUG_OWNER, settings, hours=hours, redact=True, names=[])
