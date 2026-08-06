"""Plan auto-continuation against real Postgres (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

Covers the continuation bookkeeping on `agent_session_plans`: scheduling + atomic
claim, the guards that keep a claim from firing (awaiting-owner, not-in-work, over the
cap), the shared settle decision (`maybe_schedule_continuation`), the owner-message
reset, and jerv's `await_owner` opt-out through the write_plan handler.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.continuation import MAX_CONTINUATIONS, maybe_schedule_continuation
from jbrain.agent.loop import ToolContext
from jbrain.agent.plantools import build_plan_handlers
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.plan import PlanRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_OPEN = "- [ ] step one\n- [ ] step two"   # has unchecked items
_DONE = "- [x] step one\n- [x] step two"   # all checked


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
        assert (await repo.get(s, sid)).continuation_due_at is not None

    async with scoped_session(maker, owner) as s:
        claimed = await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS)
    assert claimed == [sid]

    async with scoped_session(maker, owner) as s:
        plan = await repo.get(s, sid)
        assert plan.continuation_due_at is None  # claim cleared the due-time…
        assert plan.continuations_used == 1  # …and counted the fire
        # A second sweep claims nothing (the due-time is gone).
        assert await repo.claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []


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
        return (await PlanRepo().get(s, sid)).continuation_due_at is not None


async def test_settle_helper_arms_only_when_it_should(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)

    # in-work + unchecked + clean stop → armed.
    good = await _chat(maker, owner, body=_OPEN, status="in_work")
    await maybe_schedule_continuation(maker, owner, good, agent="jerv", stop_reason="end_turn")
    assert await _due(maker, owner, good)

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


async def test_owner_message_reset_clears_everything(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _chat(maker, owner, body=_OPEN, status="in_work")
    repo = PlanRepo()
    async with scoped_session(maker, owner) as s:
        await repo.schedule_continuation(s, sid, delay_s=0)
        await repo.set_awaiting_owner(s, sid, True)
        await s.execute(
            text("UPDATE app.agent_session_plans SET continuations_used = 5 WHERE session_id = :id"),
            {"id": sid},
        )
        await repo.cancel_and_reset(s, sid)
        plan = await repo.get(s, sid)
    assert plan.continuation_due_at is None
    assert plan.awaiting_owner is False
    assert plan.continuations_used == 0


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
        assert plan.awaiting_owner is True
        assert plan.continuation_due_at is None  # await_owner also cancels a pending fire
        # …and a due sweep now skips it.
        assert await PlanRepo().claim_due_continuations(s, max_continuations=MAX_CONTINUATIONS) == []
