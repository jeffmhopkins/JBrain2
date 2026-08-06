"""Migration 0155 against real Postgres: `agent_session_plans` is owner-only (CLAUDE.md
rule 3), and jerv's plan state machine holds.

The mandatory per-new-table RLS isolation test. The owner round-trips a draft →
approve → in-work plan via `PlanRepo` and the tool handlers; a non-owner
(capability-token) principal sees ZERO rows and cannot write. The state machine is
exercised through the handlers: jerv can never set `approved`, and `in_work` is refused
until the owner has approved. Finally, deleting the chat cascades the plan away.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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

# A non-owner principal: a capability token with no owner identity — app.is_owner() is false.
NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


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


async def _new_chat(maker: async_sessionmaker, owner: SessionContext) -> str:
    """A jerv chat row to hang a plan off (FK target)."""
    sid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (:id, :pid, 'jerv', '{}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
    return sid


async def test_owner_roundtrips_a_plan(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    repo = PlanRepo()

    async with scoped_session(maker, owner) as session:
        assert await repo.get(session, sid) is None  # no plan before any write
        plan = await repo.upsert(session, sid, body="1. do X\n2. do Y")
        assert plan.status == "not_approved"  # a fresh draft defaults to awaiting approval

    async with scoped_session(maker, owner) as session:
        plan = await repo.set_status(session, sid, "approved")  # the owner approve path
        assert plan is not None and plan.status == "approved"
        assert plan.body == "1. do X\n2. do Y"  # approving does not disturb the body


async def test_handlers_enforce_the_state_machine(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    handlers = build_plan_handlers(maker)
    ctx = ToolContext(session=owner, scopes=(), agent_session_id=sid)

    # jerv drafts → not_approved.
    out = await handlers["write_plan"]({"body": "step 1"}, ctx)
    assert "Approve" in out and "read_plan" not in out  # the awaiting-approval note

    # jerv cannot start work before the owner approves.
    blocked = await handlers["write_plan"]({"status": "in_work"}, ctx)
    assert "isn't approved yet" in blocked
    async with scoped_session(maker, owner) as session:
        assert (await PlanRepo().get(session, sid)).status == "not_approved"  # unchanged

    # The owner approves out-of-band (the api endpoint's repo call), then jerv may work it.
    async with scoped_session(maker, owner) as session:
        await PlanRepo().set_status(session, sid, "approved")
    worked = await handlers["write_plan"]({"status": "in_work"}, ctx)
    assert "in work" in worked
    reread = await handlers["read_plan"]({}, ctx)
    assert "step 1" in reread


async def test_jerv_runtime_ctx_reaches_the_owner_only_table(maker: async_sessionmaker) -> None:
    """jerv runs owner-scoped with EMPTY domain scopes (its firewall is ownership, not a
    domain). The plan table's policy is `app.is_owner()` only, so jerv's actual runtime ctx
    must still read/write it — this proves the 'ownership-not-domain' reach with the real
    ctx shape, not just a plain owner."""
    base = await _owner(maker)
    jerv = SessionContext(
        principal_id=base.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=(),
    )
    sid = await _new_chat(maker, jerv)
    repo = PlanRepo()
    async with scoped_session(maker, jerv) as session:
        await repo.upsert(session, sid, body="jerv drafted this", status="not_approved")
    async with scoped_session(maker, jerv) as session:
        assert (await repo.get(session, sid)).body == "jerv drafted this"


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    repo = PlanRepo()

    async with scoped_session(maker, owner) as session:
        await repo.upsert(session, sid, body="owner-only plan", status="approved")

    # A non-owner principal sees zero rows — RLS hides every chat's plan.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM app.agent_session_plans"))
        ).scalar()
    assert count == 0

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await repo.upsert(session, sid, body="sneaky", status="approved")


async def test_deleting_the_chat_cascades_the_plan(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    repo = PlanRepo()

    async with scoped_session(maker, owner) as session:
        await repo.upsert(session, sid, body="to be cascaded")
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text("DELETE FROM app.agent_sessions WHERE id = :id"), {"id": sid}
        )
    async with scoped_session(maker, owner) as session:
        assert await repo.get(session, sid) is None  # ON DELETE CASCADE removed the plan
