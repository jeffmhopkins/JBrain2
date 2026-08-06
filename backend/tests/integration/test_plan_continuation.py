"""Plan auto-continuation against real Postgres (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

Covers the continuation bookkeeping on `agent_session_plans`: scheduling + atomic
claim, the guards that keep a claim from firing (awaiting-owner, not-in-work, over the
cap), the shared settle decision (`maybe_schedule_continuation`), the owner-message
reset, and jerv's `await_owner` opt-out through the write_plan handler.
"""

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.continuation import (
    MAX_CONTINUATIONS,
    PlanContinuationRunner,
    maybe_schedule_continuation,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.plantools import build_plan_handlers
from jbrain.api.agent import _plan_blocks
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.plan import PlanRepo
from jbrain.tasks.scheduler import _owner_principal_id
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_OPEN = "- [ ] step one\n- [ ] step two"  # has unchecked items
_DONE = "- [x] step one\n- [x] step two"  # all checked


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _chat(maker: async_sessionmaker, owner: SessionContext, *, body: str, status: str) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (:id, :pid, 'jerv', '{}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
        await PlanRepo().upsert(session, sid, body=body, status=status)
    return sid


async def test_schedule_then_claim_fires_once_and_counts(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()

    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, sid, delay_s=0)
        plan = await repo.get(s, sid)
        assert plan is not None and plan.continuation_due_at is not None

    async with scoped_session(maker, owner) as s:
        claimed = await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
    assert claimed == [sid]

    async with scoped_session(maker, owner) as s:
        plan = await repo.get(s, sid)
        assert plan is not None
        assert plan.continuation_due_at is None  # claim cleared the due-time…
        assert plan.continuations_used == 0  # …but the count is bumped on a real run, not claim
        # A second sweep claims nothing (the due-time is gone).
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []

    # bump_continuation (called when the claimed turn actually runs) counts the fire.
    async with scoped_session(maker, owner) as s:
        await repo.bump_continuation(s, sid)
    async with scoped_session(maker, owner) as s:
        plan = await repo.get(s, sid)
        assert plan is not None and plan.continuations_used == 1


async def test_claim_skips_awaiting_owner_and_non_in_work(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    awaiting = await _chat(maker, owner, body=_OPEN, status="in_work")
    draft = await _chat(maker, owner, body=_OPEN, status="not_approved")
    repo = PlanRepo()

    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, awaiting, delay_s=0)
        await repo.set_awaiting_owner(s, awaiting, True)  # also clears the due-time
        await repo.schedule_continuation(s, draft, delay_s=0)  # but it's not in_work

    async with scoped_session(maker, owner) as s:
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []


async def test_claim_respects_the_cap(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "UPDATE app.agent_session_plans SET continuations_used = :m WHERE session_id = :id"
            ),
            {"m": MAX_CONTINUATIONS, "id": sid},
        )
        await repo.schedule_continuation(s, sid, delay_s=0)
    async with scoped_session(maker, owner) as s:
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []


async def _due(maker: async_sessionmaker, owner: SessionContext, sid: str) -> bool:
    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get(s, sid)
        assert plan is not None
        return plan.continuation_due_at is not None


async def test_settle_helper_arms_only_when_it_should(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)

    # in-work + unchecked + clean stop → armed.
    good = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, good, agent="jerv", stop_reason="end_turn")
    assert await _due(maker, owner, good)

    # approved-but-not-yet-started + unchecked → armed too (the first step continues the
    # plan without the owner having to send another message).
    approved = await _chat(maker, owner, body=_OPEN, status="approved")
    await maybe_schedule_continuation(maker, owner, approved, agent="jerv", stop_reason="end_turn")
    assert await _due(maker, owner, approved)

    # a still-unapproved draft is never armed.
    draft = await _chat(maker, owner, body=_OPEN, status="not_approved")
    await maybe_schedule_continuation(maker, owner, draft, agent="jerv", stop_reason="end_turn")
    assert not await _due(maker, owner, draft)

    # all steps checked → not armed.
    done = await _chat(maker, owner, body=_DONE, status="in_work")
    await maybe_schedule_continuation(maker, owner, done, agent="jerv", stop_reason="end_turn")
    assert not await _due(maker, owner, done)

    # a deferred turn (handed off to a background job) → not armed.
    deferred = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, deferred, agent="jerv", stop_reason="deferred")
    assert not await _due(maker, owner, deferred)

    # a non-jerv agent → not armed.
    other = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, other, agent="curator", stop_reason="end_turn")
    assert not await _due(maker, owner, other)


class _FakeResult:
    text = "worked the step"
    stop_reason = "end_turn"
    steps = 1
    cost_tokens = 5


class _FakeExecuted:
    result = _FakeResult()
    tools: list = []
    reasoning = ""


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_turn(self, **kwargs: object) -> _FakeExecuted:
        # Record the session and the TYPE of the principal id — the uuid→str bug would
        # never reach here (tick would throw at set_config), so a call at all proves the fix.
        pid = kwargs["read_ctx"].principal_id  # type: ignore[attr-defined]
        self.calls.append({"sid": kwargs["agent_session_id"], "pid_type": type(pid).__name__})
        return _FakeExecuted()


class _FakeRunLog:
    async def start(self, ctx: object, *, session_id: str, prompt_version: str) -> str:
        return "run-1"

    def bound(self, ctx: object, run_id: str) -> object:
        return object()

    async def finish(self, ctx: object, run_id: str, **kwargs: object) -> None:
        return None


class _FakeTranscript:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def record_answer(self, ctx: object, *, session_id: str, **kwargs: object) -> None:
        self.answers.append(session_id)


async def test_sweep_tick_fires_a_due_continuation(maker: async_sessionmaker) -> None:
    """End-to-end sweep: with a real owner + an armed plan, `runner.tick()` claims and runs
    the continuation. Uses the REAL owner-pid resolver (a raw uuid) — the regression guard
    for the bug where the sweep passed that uuid straight into the RLS ctx and silently
    threw at set_config, so an approved plan sat at 'continuing in 0:00' forever."""
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="approved")
    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, sid, delay_s=0)

    executor = _FakeExecutor()
    transcript = _FakeTranscript()
    runner = PlanContinuationRunner(
        maker=maker,
        executor=executor,  # type: ignore[arg-type]
        runlog=_FakeRunLog(),  # type: ignore[arg-type]
        transcript=transcript,  # type: ignore[arg-type]
        live_turns={},
        owner_principal_id=lambda: _owner_principal_id(maker),  # raw uuid, like production
    )
    await runner.tick()

    assert [c["sid"] for c in executor.calls] == [sid]  # claimed + ran (didn't throw)
    assert executor.calls[0]["pid_type"] == "str"  # the RLS ctx got a str, not a uuid
    assert transcript.answers == [sid]  # recorded answer-only
    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get(s, sid)
        assert plan is not None and plan.continuations_used == 1  # the fire was counted


async def test_approving_arms_and_claims_the_first_step(maker: async_sessionmaker) -> None:
    """The fix for 'approved but sitting there': the approve endpoint arms a continuation,
    and the sweep claims an `approved` plan (not just `in_work`) — so the first step starts
    on its own. jerv then flips the status to in_work as it executes."""
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="approved")
    repo = PlanRepo()
    # Mirror the approve endpoint: arm the first continuation on the just-approved plan.
    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, sid, delay_s=0)
        plan = await repo.get(s, sid)
        assert plan is not None and plan.continuation_due_at is not None
    # The sweep claims it even though the status is still `approved` (jerv hasn't started).
    async with scoped_session(maker, owner) as s:
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == [sid]


async def test_owner_message_reset_clears_everything(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()
    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, sid, delay_s=0)
        await repo.set_awaiting_owner(s, sid, True)
        await s.execute(
            text(
                "UPDATE app.agent_session_plans SET continuations_used = 5 WHERE session_id = :id"
            ),
            {"id": sid},
        )
        await repo.cancel_and_reset(s, sid)
        plan = await repo.get(s, sid)
        assert plan is not None
    assert plan.continuation_due_at is None
    assert plan.awaiting_owner is False
    assert plan.continuations_used == 0


async def test_plan_blocks_only_injects_a_sanctioned_plan(maker: async_sessionmaker) -> None:
    """The re-injection rule: a not_approved DRAFT is never fed to the turn as a sanctioned
    operating instruction (that's what stops an injection-drafted plan being framed as
    'follow this'); only an owner-approved / in-work plan is, and only for a jerv session."""
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body="- [ ] do the thing", status="not_approved")
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)))
    jerv = SimpleNamespace(agent="jerv", id=sid)

    # A draft is NOT injected.
    assert await _plan_blocks(req, owner, jerv) == []  # type: ignore[arg-type]

    # Once the owner approves, it IS injected — a DATA-framed operating block.
    async with scoped_session(maker, owner) as s:
        await PlanRepo().set_status(s, sid, "approved")
    blocks = await _plan_blocks(req, owner, jerv)  # type: ignore[arg-type]
    assert len(blocks) == 1
    assert "APPROVED" in blocks[0].text and "do the thing" in blocks[0].text  # type: ignore[union-attr]

    # A non-jerv agent never receives it.
    assert await _plan_blocks(req, owner, SimpleNamespace(agent="curator", id=sid)) == []  # type: ignore[arg-type]


async def test_write_plan_await_owner_stops_the_loop(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="in_work")
    handlers = build_plan_handlers(maker)
    ctx = ToolContext(session=owner, scopes=(), agent_session_id=sid)

    async with scoped_session(maker, owner) as s:
        await PlanRepo().schedule_continuation(s, sid, delay_s=0)

    out = await handlers["write_plan"]({"pause": "await_owner"}, ctx)
    assert "wait for your reply" in out

    async with scoped_session(maker, owner) as s:
        plan = await PlanRepo().get(s, sid)
        assert plan is not None
        assert plan.awaiting_owner is True
        assert plan.continuation_due_at is None  # await_owner also cancels a pending fire
        # …and a due sweep now skips it.
        claimed = await PlanRepo().claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
        assert claimed == []
