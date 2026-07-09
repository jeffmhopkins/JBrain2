"""The Runs API — the owner-only run-log surface behind the Ops "Runs" screen
(docs/archive/WORKFLOW_ENGINE_PLAN.md §5 Track D).

List the recent runs (each a glanceable summary), and open one run to its
ordered step tree. Reads only; the run log is owner-only (RLS), so every read
runs under the owner session — the firewall, not this code, is what keeps the
log private. The sweep/emergency-trigger control the dashboard renders fires
`POST /api/ops/triggers/{id}/run` (sibling task B); this router never mutates.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from jbrain.agent.runlog import RunLogReader
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo

router = APIRouter(prefix="/runs", dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]

# A generous-but-bounded recency window: the dashboard shows the live log, not
# the full history (the audit trail is queried elsewhere). The client may ask for
# fewer, or more up to MAX_LIMIT once it filters server-side (e.g. the last 200
# agent turns), but never an unbounded scan.
RECENT_LIMIT = 50
MAX_LIMIT = 200


def get_run_reader(request: Request) -> RunLogReader:
    return cast(RunLogReader, request.app.state.run_reader)


class RunSummaryOut(BaseModel):
    id: str
    kind: str
    status: str
    name: str
    started_at: datetime
    duration_ms: int | None
    step_count: int
    cost_tokens: int
    last_error: str | None
    progress_note: str | None


class QueueDepthOut(BaseModel):
    # Jobs waiting in app.jobs (status='queued') — the dashboard "jobs queued" tile.
    queued: int


class RunStatsOut(BaseModel):
    # The dashboard's tile + chip-count aggregates (computed over the whole log, not
    # the fetched page). Tiles are today/now; by_kind respects the active filters.
    active: int
    failed_today: int
    tokens_today: int
    by_kind: dict[str, int]


class RunStepOut(BaseModel):
    idx: int
    kind: str
    name: str
    ok: bool
    cost_tokens: int
    job_id: str | None
    error: str | None
    # The step's captured structured-log trace (the "full logs" review view), or
    # null for a step that recorded none.
    detail: list[dict[str, object]] | None = None


class RunDetailOut(BaseModel):
    id: str
    kind: str
    status: str
    name: str
    started_at: datetime
    duration_ms: int | None
    step_count: int
    cost_tokens: int
    stop_reason: str | None
    progress_note: str | None
    steps: list[RunStepOut]


@router.get("")
async def list_runs(
    request: Request,
    principal: OwnerDep,
    kinds: Annotated[list[str] | None, Query()] = None,
    exclude_sweeps: bool = False,
    since: datetime | None = None,
    limit: int = RECENT_LIMIT,
) -> list[RunSummaryOut]:
    reader = get_run_reader(request)
    runs = await reader.list_recent(
        ctx_for(principal),
        limit=max(1, min(limit, MAX_LIMIT)),
        kinds=kinds,
        exclude_sweeps=exclude_sweeps,
        since=since,
    )
    return [RunSummaryOut(**vars(r)) for r in runs]


# Declared before "/{run_id}" so the literal path wins the route match (otherwise
# "queue-depth"/"stats" are captured as a run id and 404).
@router.get("/queue-depth")
async def queue_depth(request: Request, principal: OwnerDep) -> QueueDepthOut:
    reader = get_run_reader(request)
    return QueueDepthOut(queued=await reader.queue_depth(ctx_for(principal)))


@router.get("/stats")
async def run_stats(
    request: Request,
    principal: OwnerDep,
    exclude_sweeps: bool = False,
    since: datetime | None = None,
) -> RunStatsOut:
    reader = get_run_reader(request)
    stats = await reader.stats(ctx_for(principal), since=since, exclude_sweeps=exclude_sweeps)
    return RunStatsOut(**vars(stats))


@router.get("/{run_id}")
async def get_run(request: Request, principal: OwnerDep, run_id: str) -> RunDetailOut:
    reader = get_run_reader(request)
    detail = await reader.load(ctx_for(principal), run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="no run with that id in scope")
    return RunDetailOut(
        **{k: v for k, v in vars(detail).items() if k != "steps"},
        steps=[RunStepOut(**vars(s)) for s in detail.steps],
    )
