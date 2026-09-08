"""Migration 0188 against real Postgres: RLS isolation for `app.graph_rebuild_runs`
(CLAUDE.md rule 3 — every new table gets one).

The owner/system posture (`app.is_owner()`, the triggers/schedules precedent): the row
is sweep metadata with no note content and no domain of its own, so every owner session
— narrowed or not — sees its own maintenance state, and no capability-token session
sees it at all, nor can one write one.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
# A narrowed owner session (0015): owner identity, firewalled to its domain scopes. An
# owner-only table stays visible to it — the agent_runs precedent.
OWNER_HEALTH = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("health",),
    owner_scoped=True,
)

COUNT = "SELECT count(*) FROM app.graph_rebuild_runs WHERE id = CAST(:id AS uuid)"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed_run(maker: async_sessionmaker) -> str:
    run_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.graph_rebuild_runs (id, status, total_notes)"
                " VALUES (CAST(:id AS uuid), 'completed', 7)"
            ),
            {"id": run_id},
        )
    return run_id


async def visible(maker: async_sessionmaker, ctx: SessionContext, run_id: str) -> int:
    async with scoped_session(maker, ctx) as s:
        return int((await s.execute(text(COUNT), {"id": run_id})).scalar_one())


async def test_run_rows_are_owner_only(maker: async_sessionmaker) -> None:
    run_id = await seed_run(maker)
    assert await visible(maker, OWNER, run_id) == 1
    assert await visible(maker, OWNER_HEALTH, run_id) == 1
    assert await visible(maker, HEALTH_ONLY, run_id) == 0
    assert await visible(maker, UNSCOPED, run_id) == 0


async def test_a_token_session_cannot_open_a_rebuild(maker: async_sessionmaker) -> None:
    """The write side matters more than the read side: a rebuild purges the whole
    corpus's derived graph, so nothing but an owner/system context may open one."""
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, HEALTH_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.graph_rebuild_runs (status, total_notes)"
                    " VALUES ('purging', 999)"
                )
            )


async def test_run_rows_are_not_deletable(maker: async_sessionmaker) -> None:
    """A finished run is audit history like `eval_runs`: the sweep supersedes it by
    opening a new one. No DELETE grant, so even the owner session cannot erase one."""
    run_id = await seed_run(maker)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text("DELETE FROM app.graph_rebuild_runs WHERE id = CAST(:id AS uuid)"),
                {"id": run_id},
            )


async def test_only_one_run_may_be_open(maker: async_sessionmaker) -> None:
    """The partial unique index is what makes two concurrent "Run now" clicks safe: a
    second run over the same corpus would re-purge from a stale cursor."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.graph_rebuild_runs (status, total_notes) VALUES ('purging', 1)")
        )
    try:
        with pytest.raises(Exception, match="graph_rebuild_runs_one_active"):
            async with scoped_session(maker, OWNER) as s:
                await s.execute(
                    text(
                        "INSERT INTO app.graph_rebuild_runs (status, total_notes)"
                        " VALUES ('draining', 1)"
                    )
                )
    finally:
        # The index is corpus-wide and the test database is shared: an open run left
        # behind here makes every later `start_run` a silent no-op. There is no DELETE
        # grant (a run row is audit history), so close it instead.
        async with scoped_session(maker, OWNER) as s:
            await s.execute(text("UPDATE app.graph_rebuild_runs SET status = 'completed'"))
