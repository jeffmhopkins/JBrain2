"""Migration 0220 against real Postgres: `llm_engine_effort` is owner-only and jmolt-denied
(CLAUDE.md rule 3) — the mandatory per-new-table RLS isolation test.

The owner's settings screen writes under its own principal and the router reads under the
system context (`queue.SYSTEM_CTX`, principal `worker`); both are owners, so both must see the
same rows — that is why the table has no principal column. A capability-token principal and
jmolt's nightly context (owner kind, `auth_context='jmolt'`) must see nothing and write nothing.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_night import jmolt_run_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
FLASH = "flash-next"


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
        # The database is per module, so each test starts from an empty table.
        await session.execute(text("DELETE FROM app.llm_engine_effort"))
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _count(maker: async_sessionmaker, ctx: SessionContext) -> int:
    async with scoped_session(maker, ctx) as session:
        return (
            await session.execute(text("SELECT count(*) FROM app.llm_engine_effort"))
        ).scalar_one()


async def test_the_owner_writes_and_the_routers_system_context_reads(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    store = SqlSettingsStore(maker)
    await store.set_llm_engine_efforts(
        owner,
        {(FLASH, "tier", "medium"): "low", (FLASH, "task", "agent.turn"): "high"},
    )
    # The router's read, under a different owner principal, sees the owner's rows.
    efforts = await store.llm_engine_efforts(SYSTEM_CTX)
    assert efforts.resolve(FLASH, "agent.turn", "medium") == ("high", "task")
    assert efforts.resolve(FLASH, "wiki.rewrite", "medium") == ("low", "tier")

    # An upsert replaces in place; None deletes; one transaction for the batch.
    await store.set_llm_engine_efforts(
        owner, {(FLASH, "task", "agent.turn"): None, (FLASH, "tier", "medium"): "none"}
    )
    efforts = await store.llm_engine_efforts(owner)
    assert dict(efforts.rows) == {(FLASH, "tier", "medium"): "none"}


async def test_a_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = SqlSettingsStore(maker)
    await store.set_llm_engine_efforts(owner, {(FLASH, "tier", "high"): "high"})

    assert await _count(maker, NON_OWNER) == 0
    assert (await store.llm_engine_efforts(NON_OWNER)).rows == {}
    with pytest.raises(ProgrammingError):
        await store.set_llm_engine_efforts(NON_OWNER, {(FLASH, "tier", "low"): "none"})
    # A delete RLS hides from is a silent no-op, never a write.
    await store.set_llm_engine_efforts(NON_OWNER, {(FLASH, "tier", "high"): None})

    assert dict((await store.llm_engine_efforts(owner)).rows) == {(FLASH, "tier", "high"): "high"}


async def test_jmolts_context_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    assert owner.principal_id is not None
    jmolt = jmolt_run_context(owner.principal_id)
    store = SqlSettingsStore(maker)
    await store.set_llm_engine_efforts(owner, {(FLASH, "tier", "high"): "high"})

    assert await _count(maker, jmolt) == 0
    with pytest.raises(ProgrammingError):
        await store.set_llm_engine_efforts(jmolt, {(FLASH, "task", "agent.turn"): "none"})
    assert dict((await store.llm_engine_efforts(owner)).rows) == {(FLASH, "tier", "high"): "high"}


async def test_the_checks_refuse_an_unknown_scope_or_level(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    for scope, effort in (("model", "low"), ("task", "xhigh")):
        with pytest.raises(DBAPIError):
            async with scoped_session(maker, owner) as session:
                await session.execute(
                    text(
                        "INSERT INTO app.llm_engine_effort (engine, scope, key, effort)"
                        " VALUES ('flash-next', :scope, 'agent.turn', :effort)"
                    ),
                    {"scope": scope, "effort": effort},
                )
    assert await _count(maker, owner) == 0
