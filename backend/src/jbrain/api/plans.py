"""The per-conversation Plan API — the owner's approve/edit surface for a jerv plan
(docs/plans/JERV_PLANNING_TOOL_PLAN.md). Owner-only.

jerv drafts and rewrites plans through its `write_plan` tool, but the `approved`
transition is the OWNER's alone — so web content jerv reads can never talk it into
self-approving. A conversation can hold several plans over its life; each is addressed by
its own `plan_id`, so an old plan's inline card keeps operating on its own row while the
omnibox tracks the session's ACTIVE (latest) plan. These endpoints back the inline
`plan_card` and the omnibox popover: read a plan, resolve the active one, approve it, or
correct its text before approving. All owner-only, RLS-scoped to `app.is_owner()`.
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
    # `plan_id` is the plan's own identity (a card polls/mutates by it); `session_id` is the
    # conversation it belongs to (many plans may share one).
    plan_id: str
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
        plan_id=str(plan.plan_id),
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


@router.get("/session/{session_id}/active")
async def get_active_plan(request: Request, principal: OwnerDep, session_id: str) -> PlanOut:
    """The conversation's ACTIVE (latest-created) plan — what the omnibox pill/popover tracks.
    404 if the conversation has no plan yet."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.get_active(db, session_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="no plan for that conversation")
    return _out(plan)


@router.get("/{plan_id}")
async def get_plan(request: Request, principal: OwnerDep, plan_id: str) -> PlanOut:
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.get_by_id(db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="no such plan")
    return _out(plan)


@router.post("/{plan_id}/approve")
async def approve_plan(request: Request, principal: OwnerDep, plan_id: str) -> PlanOut:
    """Sign off on the plan — the ONE transition jerv cannot make itself. 404 if there is
    no such plan (the owner can't approve a plan that was never drafted).

    Approval also ARMS the first continuation: the sweep then starts jerv on the plan on
    its next pass (~15s). Without this, approval alone just sets the status and the plan
    sits — nothing tells the agent it was approved."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        # `approve` also clears any stale await-owner (jerv may have paused the draft), so the
        # continuation armed below can actually be claimed — otherwise the approved plan sits.
        approved = await _repo.approve(db, plan_id)
        if approved is None:
            raise HTTPException(status_code=404, detail="no such plan")
        # Only ARM the loop if this is the conversation's ACTIVE plan. Approving a superseded
        # plan (a newer plan was drafted after this card) would arm a continuation the sweep's
        # active-plan guard then drops — a countdown that silently never runs. The plan still
        # becomes `approved`; it just won't auto-run while a newer plan owns the conversation.
        is_active = await _repo.active_plan_id(db, str(approved.session_id)) == plan_id
        if is_active:
            await _repo.schedule_continuation(db, plan_id, delay_s=0)
    # Start the first step NOW instead of waiting for the next periodic sweep — the owner
    # expects Approve to kick off step one immediately, not after a window.
    if is_active:
        _kick_continuations(request)
    # Re-read in a fresh session so the returned state reflects the armed due-time.
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.get_by_id(db, plan_id)
    assert plan is not None
    return _out(plan)


@router.post("/{plan_id}/edit")
async def edit_plan(request: Request, principal: OwnerDep, plan_id: str, body: EditIn) -> PlanOut:
    """Correct the plan text in place before approving. 404 if there is no such plan —
    the owner corrects jerv's draft, they don't author one from scratch here."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        plan = await _repo.update_plan(db, plan_id, body=body.body)
    if plan is None:
        raise HTTPException(status_code=404, detail="no such plan")
    return _out(plan)


@router.post("/{plan_id}/stop")
async def stop_plan(request: Request, principal: OwnerDep, plan_id: str) -> PlanOut:
    """Cancel the pending auto-continuation and reset its budget — the owner halting the
    loop from the card. 404 if there is no such plan."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        if await _repo.get_by_id(db, plan_id) is None:
            raise HTTPException(status_code=404, detail="no such plan")
        await _repo.cancel_and_reset(db, plan_id)
        plan = await _repo.get_by_id(db, plan_id)
    assert plan is not None
    return _out(plan)


@router.post("/{plan_id}/continue")
async def continue_plan(request: Request, principal: OwnerDep, plan_id: str) -> PlanOut:
    """Fire the next step now instead of waiting out the window — arms the continuation
    due immediately (the sweep picks it up on its next pass). 404 if there is no such plan."""
    async with scoped_session(request.app.state.session_maker, ctx_for(principal)) as db:
        if (plan := await _repo.get_by_id(db, plan_id)) is None:
            raise HTTPException(status_code=404, detail="no such plan")
        # Only arm it if it would actually fire: in-work, not paused for the owner, under the
        # cap, AND the session's ACTIVE plan (else the sweep's own `used < max` / active-plan
        # filters would never run it, so the card would show a countdown that silently never
        # runs).
        armed = (
            plan.status in ("approved", "in_work")
            and not plan.awaiting_owner
            and plan.continuations_used < MAX_CONTINUATIONS
            and await _repo.active_plan_id(db, str(plan.session_id)) == plan_id
        )
        if armed:
            await _repo.schedule_continuation(db, plan_id, delay_s=0)
        plan = await _repo.get_by_id(db, plan_id)
    # Fire it off now (not on the next sweep) so "Continue now" is actually now.
    if armed:
        _kick_continuations(request)
    assert plan is not None
    return _out(plan)
