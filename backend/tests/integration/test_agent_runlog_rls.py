"""Migrations 0016+0037 against real Postgres: the agent run log persists a run
and its steps into the unified `runs`/`run_steps` tables, stamps `kind='agent'`,
and both tables stay owner-only after the rename (CLAUDE.md rule 3)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

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
