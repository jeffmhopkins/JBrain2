"""The Runs API — the owner-only run-log surface behind the Ops "Runs" screen
(docs/WORKFLOW_ENGINE_PLAN.md §5 Track D).

List the recent runs (each a glanceable summary), and open one run to its
ordered step tree. Reads only; the run log is owner-only (RLS), so every read
runs under the owner session — the firewall, not this code, is what keeps the
log private. The sweep/emergency-trigger control the dashboard renders fires
`POST /api/ops/triggers/{id}/run` (sibling task B); this router never mutates.
"""

from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.agent.runlog import RunLogReader
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo

router = APIRouter(prefix="/runs", dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]

# A generous-but-bounded recency window: the dashboard shows the live log, not
# the full history (the audit trail is queried elsewhere).
RECENT_LIMIT = 50


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
async def list_runs(request: Request, principal: OwnerDep) -> list[RunSummaryOut]:
    reader = get_run_reader(request)
    runs = await reader.list_recent(ctx_for(principal), limit=RECENT_LIMIT)
    return [RunSummaryOut(**vars(r)) for r in runs]


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
