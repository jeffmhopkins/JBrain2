"""Migrations 0016+0037 against real Postgres: the agent run log persists a run
and its steps into the unified `runs`/`run_steps` tables, stamps `kind='agent'`,
and both tables stay owner-only after the rename (CLAUDE.md rule 3)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


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


async def test_run_log_persists_and_is_owner_only(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=["general"], title="ask")

    log = AgentRunLog(maker)
    run_id = await log.start(owner, session_id=info.id, prompt_version="agent-system-v1")
    await log.step(owner, run_id, idx=0, kind="model", name="converse", ok=True, cost_tokens=15)
    await log.step(owner, run_id, idx=1, kind="tool", name="search", ok=True, cost_tokens=0)
    await log.finish(
        owner, run_id, status="done", stop_reason="end_turn", step_count=2, cost_tokens=15
    )

    async with scoped_session(maker, owner) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, step_count, cost_tokens, prompt_version, kind, ran_as"
                    " FROM app.runs WHERE id = :id"
                ),
                {"id": run_id},
            )
        ).one()
        # The run lands stamped as an agent run, scoped (not owner-system).
        assert row == ("done", 2, 15, "agent-system-v1", "agent", "scoped")
        steps = (
            await session.execute(
                text("SELECT count(*) FROM app.run_steps WHERE run_id = :id"), {"id": run_id}
            )
        ).scalar()
        assert steps == 2

    # Owner-only: a non-owner principal sees no runs or steps after the rename.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, token) as session:
        assert (await session.execute(text("SELECT count(*) FROM app.runs"))).scalar() == 0
        assert (await session.execute(text("SELECT count(*) FROM app.run_steps"))).scalar() == 0


async def test_run_step_job_id_db_fk_sets_null_when_job_ages_out(
    maker: async_sessionmaker,
    database_url: str,  # noqa: F811
) -> None:
    """N3: RunStep.job_id has NO ORM FK (app.jobs is the queue.py raw-SQL substrate,
    not a mapped table — an ORM FK would fail mapper resolution), but the DB-level FK
    exists ON DELETE SET NULL (migration 0037). Assert that behavior directly: a
    run_step referencing a job, then the job row removed from app.jobs, leaves the
    run_step intact with job_id NULL — a run-log read never breaks (not a dangling FK,
    not a deleted run_step). The app role has no DELETE on app.jobs (the queue only
    parks rows as done/failed), so the row removal is done via the admin role — the
    point is the DB FK's referential action, not who removes the row."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=["general"], title="fk")

    log = AgentRunLog(maker)
    run_id = await log.start(owner, session_id=info.id, prompt_version="agent-system-v1")

    async with scoped_session(maker, owner) as session:
        # A real app.jobs row the step references — the DB FK requires an existing job.
        job_id = (
            await session.execute(
                text(
                    "INSERT INTO app.jobs (id, kind, payload)"
                    " VALUES (gen_random_uuid(), 'integrate_note', '{}'::jsonb)"
                    " RETURNING id::text"
                )
            )
        ).scalar_one()
        step_id = (
            await session.execute(
                text(
                    "INSERT INTO app.run_steps (id, run_id, idx, kind, name, job_id, ok)"
                    " VALUES (gen_random_uuid(), :rid, 0, 'action', 'enqueue',"
                    " cast(:jid AS uuid), true) RETURNING id::text"
                ),
                {"rid": run_id, "jid": job_id},
            )
        ).scalar_one()

    # The job row is removed from app.jobs (the FK trigger fires regardless of who
    # deletes it). Use the admin role since the app role intentionally lacks DELETE.
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    admin = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with admin.begin() as conn:
            await conn.execute(
                text("DELETE FROM app.jobs WHERE id = cast(:jid AS uuid)"), {"jid": job_id}
            )
    finally:
        await admin.dispose()

    # The run_step survives with job_id nulled — ON DELETE SET NULL, not CASCADE.
    async with scoped_session(maker, owner) as session:
        row = (
            await session.execute(
                text("SELECT job_id FROM app.run_steps WHERE id = cast(:sid AS uuid)"),
                {"sid": step_id},
            )
        ).one()
    assert row.job_id is None


async def test_chat_finalization_statuses_satisfy_the_runs_constraint(
    maker: async_sessionmaker,
) -> None:
    """The /chat endpoint closes a run with one of exactly three statuses — `done` (the turn
    completed), `error` (a mid-stream failure OR a client disconnect) — and the runs status CHECK
    (migration 0016) must accept them, since `finish()` is shielded + suppressed in the endpoint
    and would otherwise strand the run at `running`. This pins that vocabulary to the constraint
    (and to the frontend RunStatus = running|done|error): a regression to the old invalid
    `ended`/`cancelled`/`failed` strings is caught here, not silently swallowed in production."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    log = AgentRunLog(maker)

    for status, stop_reason in (("done", "end_turn"), ("error", "disconnected")):
        info = await sessions.create(owner, domain_scopes=["general"], title="t")
        run_id = await log.start(owner, session_id=info.id, prompt_version="agent-system-v1")
        await log.finish(
            owner, run_id, status=status, stop_reason=stop_reason, step_count=1, cost_tokens=1
        )
        async with scoped_session(maker, owner) as session:
            got = (
                await session.execute(
                    text("SELECT status FROM app.runs WHERE id = :id"), {"id": run_id}
                )
            ).scalar_one()
        assert got == status  # persisted, not stranded at 'running'

    # The old vocabulary the endpoint used to write violates the constraint — guards the fix.
    info = await sessions.create(owner, domain_scopes=["general"], title="bad")
    run_id = await log.start(owner, session_id=info.id, prompt_version="agent-system-v1")
    with pytest.raises(IntegrityError):
        await log.finish(
            owner, run_id, status="ended", stop_reason="end_turn", step_count=1, cost_tokens=1
        )
