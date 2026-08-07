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

from jbrain.agent.continuation import MAX_CONTINUATIONS
from jbrain.api.deps import owner_only
from jbrain.api.notes import ctx_for
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import scoped_session
from jbrain.models.plan import AgentSessionPlan, PlanRepo

router = APIRouter(prefix="/plans", dependencies=[Depends(owner_only)])

OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]

_repo = PlanRepo()


def _kick_continuations(request: Request) -> None:
    """Nudge the plan-continuation runner to run any now-due step immediately, so Approve /
    Continue starts the step without waiting for the next periodic sweep. Best-effort: the
    runner isn't present in some test apps, and the atomic claim keeps this from double-running
    with the periodic sweep."""
    runner = getattr(request.app.state, "plan_continuation_runner", None)
    if runner is not None:
        runner.schedule_kick()


class PlanOut(BaseModel):
    session_id: str
    body: str
    status: str
    updated_at: str
    # The append-only step-results scratchpad: `{heading?, note}` entries in append order,
    # rendered as the card's per-step Results section.
    results: list[dict] = []
    # Auto-continuation state, for the card's countdown + controls. `continuation_due_at`
    # is when the next step auto-fires (null = none pending); `awaiting_owner` means jerv
    # paused for input; the counters bound the chain.
    continuation_due_at: str | None = None
    awaiting_owner: bool = False
    continuations_used: int = 0
    max_continuations: int = MAX_CONTINUATIONS


class EditIn(BaseModel):
    # The owner's corrected plan text (correct-in-place before approving).
    body: str


def _out(plan: AgentSessionPlan) -> PlanOut:
    return PlanOut(
        session_id=str(plan.session_id),
        body=plan.body,
        status=plan.status,
        updated_at=plan.updated_at.isoformat(),
        results=list(plan.results or []),
        continuation_due_at=(
            plan.continuation_due_at.isoformat() if plan.continuation_due_at else None
        ),
        awaiting_owner=plan.awaiting_owner,
        continuations_used=plan.continuations_used,
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
    no plan to approve (the owner can't approve a plan that was never drafted).

    Approval also ARMS the first continuation: the sweep then starts jerv on the plan on
    its next pass (~15s). Without this, approval alone just sets the status and the plan
    sits — nothing tells the agent it was approved."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        # `approve` also clears any stale await-owner (jerv may have paused the draft), so the
        # continuation armed below can actually be claimed — otherwise the approved plan sits.
        if await _repo.approve(db, session_id) is None:
            raise HTTPException(status_code=404, detail="no plan for that conversation")
        await _repo.schedule_continuation(db, session_id, delay_s=0)
    # Start the first step NOW instead of waiting for the next periodic sweep — the owner
    # expects Approve to kick off step one immediately, not after a window.
    _kick_continuations(request)
    # Re-read in a fresh session so the returned state reflects the armed due-time.
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.get(db, session_id)
    assert plan is not None
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


@router.post("/{session_id}/stop")
async def stop_plan(request: Request, principal: OwnerDep, session_id: str) -> PlanOut:
    """Cancel the pending auto-continuation and reset its budget — the owner halting the
    loop from the card. 404 if there is no plan."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        if (plan := await _repo.get(db, session_id)) is None:
            raise HTTPException(status_code=404, detail="no plan for that conversation")
        await _repo.cancel_and_reset(db, session_id)
        plan = await _repo.get(db, session_id)
    assert plan is not None
    return _out(plan)


@router.post("/{session_id}/continue")
async def continue_plan(request: Request, principal: OwnerDep, session_id: str) -> PlanOut:
    """Fire the next step now instead of waiting out the window — arms the continuation
    due immediately (the sweep picks it up on its next pass). 404 if there is no plan."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        if (plan := await _repo.get(db, session_id)) is None:
            raise HTTPException(status_code=404, detail="no plan for that conversation")
        # Only arm it if it would actually fire: in-work, not paused for the owner, and
        # under the cap (else the sweep's own `used < max` filter would never claim it, so
        # the card would show a countdown that silently never runs).
        armed = (
            plan.status in ("approved", "in_work")
            and not plan.awaiting_owner
            and plan.continuations_used < MAX_CONTINUATIONS
        )
        if armed:
            await _repo.schedule_continuation(db, session_id, delay_s=0)
        plan = await _repo.get(db, session_id)
    # Fire it off now (not on the next sweep) so "Continue now" is actually now.
    if armed:
        _kick_continuations(request)
    assert plan is not None
    return _out(plan)
