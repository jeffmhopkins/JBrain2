"""The per-conversation Plan API — the owner's approve/edit surface for a jerv plan
(docs/plans/JERV_PLANNING_TOOL_PLAN.md). Owner-only.

jerv drafts and rewrites the plan through its `write_plan` tool, but the `approved`
transition is the OWNER's alone — so web content jerv reads can never talk it into
self-approving. These endpoints back the inline `plan_card`: read the plan, approve it,
or correct its text before approving. All owner-only, RLS-scoped to `app.is_owner()`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import scoped_session
from jbrain.models.plan import AgentSessionPlan, PlanRepo

router = APIRouter(prefix="/plans", dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]

_repo = PlanRepo()


class PlanOut(BaseModel):
    session_id: str
    body: str
    status: str
    updated_at: str


class EditIn(BaseModel):
    # The owner's corrected plan text (correct-in-place before approving).
    body: str


def _out(plan: AgentSessionPlan) -> PlanOut:
    return PlanOut(
        session_id=str(plan.session_id),
        body=plan.body,
        status=plan.status,
        updated_at=plan.updated_at.isoformat(),
    )


@router.get("/{session_id}")
async def get_plan(request: Request, principal: OwnerDep, session_id: str) -> PlanOut:
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.get(db, session_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="no plan for that conversation")
    return _out(plan)


@router.post("/{session_id}/approve")
async def approve_plan(request: Request, principal: OwnerDep, session_id: str) -> PlanOut:
    """Sign off on the plan — the ONE transition jerv cannot make itself. 404 if there is
    no plan to approve (the owner can't approve a plan that was never drafted)."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.set_status(db, session_id, "approved")
    if plan is None:
        raise HTTPException(status_code=404, detail="no plan for that conversation")
    return _out(plan)


@router.post("/{session_id}/edit")
async def edit_plan(
    request: Request, principal: OwnerDep, session_id: str, body: EditIn
) -> PlanOut:
    """Correct the plan text in place before approving. 404 if there is no plan yet —
    the owner edits jerv's draft, they don't author one from scratch here."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        if await _repo.get(db, session_id) is None:
            raise HTTPException(status_code=404, detail="no plan for that conversation")
        plan = await _repo.upsert(db, session_id, body=body.body)
    return _out(plan)
