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
        plan = await PlanRepo().get(session, sid)
        assert plan is not None and plan.status == "not_approved"  # unchanged

    # The owner approves out-of-band (the api endpoint's repo call), then jerv may work it.
    async with scoped_session(maker, owner) as session:
        await PlanRepo().set_status(session, sid, "approved")
    worked = await handlers["write_plan"]({"status": "in_work"}, ctx)
    assert "in work" in worked
    reread = await handlers["read_plan"]({}, ctx)
    assert "step 1" in reread


async def test_pausing_a_draft_does_not_set_awaiting_owner(maker: async_sessionmaker) -> None:
    """Regression: jerv drafting a plan AND asking to await the owner (a natural read of
    'don't start until I approve') must NOT set awaiting_owner on the not_approved draft — a
    draft already waits for approval, and the flag would hide the Approve control and block the
    loop even after approval. The pause is a no-op; the note teaches jerv why."""
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    handlers = build_plan_handlers(maker)
    ctx = ToolContext(session=owner, scopes=(), agent_session_id=sid)

    out = await handlers["write_plan"]({"body": "- [ ] step 1", "pause": "await_owner"}, ctx)
    assert "Approve" in out  # the awaiting-approval note, not the "paused for you" note
    assert "no need to pause" in out  # teaches the model the pause was ignored
    async with scoped_session(maker, owner) as session:
        plan = await PlanRepo().get(session, sid)
        assert plan is not None
        assert plan.status == "not_approved"
        assert plan.awaiting_owner is False  # the harmful flag was never set


async def test_results_scratchpad_is_append_only(maker: async_sessionmaker) -> None:
    """The step-results scratchpad appends in order and NEVER overwrites an earlier entry —
    so no later step can erase a prior step's results from view. Covered through the repo and
    the write_plan_result handler; read_plan surfaces the whole scratchpad."""
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    repo = PlanRepo()
    handlers = build_plan_handlers(maker)
    ctx = ToolContext(session=owner, scopes=(), agent_session_id=sid)

    async with scoped_session(maker, owner) as session:
        assert await repo.append_result(session, sid, note="x") is None  # no plan yet
        await repo.upsert(session, sid, body="- [ ] one\n- [ ] two", status="approved")

    # The handler appends; two calls yield two ordered entries (the second never clobbers).
    out1 = await handlers["write_plan_result"]({"heading": "Step 1", "note": "found A"}, ctx)
    assert "Recorded result #1" in out1
    out2 = await handlers["write_plan_result"]({"note": "found B"}, ctx)
    assert "Recorded result #2" in out2

    async with scoped_session(maker, owner) as session:
        plan = await repo.get(session, sid)
        assert plan is not None
        assert plan.results == [
            {"heading": "Step 1", "note": "found A"},
            {"note": "found B"},
        ]

    # read_plan surfaces the whole scratchpad to the model.
    read = await handlers["read_plan"]({}, ctx)
    assert "Step results recorded so far" in read and "found A" in read and "found B" in read

    # A bare/empty note is refused; a missing plan can't take a result.
    assert "needs a `note`" in await handlers["write_plan_result"]({"note": "  "}, ctx)


async def test_approve_clears_a_stale_awaiting_owner(maker: async_sessionmaker) -> None:
    """The owner's approve transition clears any leftover awaiting_owner — otherwise the first
    continuation the approve endpoint arms would never be claimed (the sweep skips
    awaiting-owner plans), leaving an approved plan that never starts."""
    owner = await _owner(maker)
    sid = await _new_chat(maker, owner)
    repo = PlanRepo()
    async with scoped_session(maker, owner) as session:
        await repo.upsert(session, sid, body="- [ ] step 1", status="not_approved")
        await repo.set_awaiting_owner(session, sid, True)  # a stale flag on the draft

    async with scoped_session(maker, owner) as session:
        plan = await repo.approve(session, sid)
        assert plan is not None
        assert plan.status == "approved"
        assert plan.awaiting_owner is False  # approving cleared it, so the loop can start

    # Approving a plan that was never drafted still 404s (None), like set_status.
    async with scoped_session(maker, owner) as session:
        assert await repo.approve(session, str(uuid.uuid4())) is None


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
        plan = await repo.get(session, sid)
        assert plan is not None and plan.body == "jerv drafted this"


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
        await session.execute(text("DELETE FROM app.agent_sessions WHERE id = :id"), {"id": sid})
    async with scoped_session(maker, owner) as session:
        assert await repo.get(session, sid) is None  # ON DELETE CASCADE removed the plan
