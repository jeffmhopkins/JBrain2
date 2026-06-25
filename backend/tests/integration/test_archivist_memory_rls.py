"""Migration 0094 against real Postgres: `archivist_memory` is owner-only (CLAUDE.md
rule 3).

The mandatory per-new-table RLS isolation test for the archivist's scratchpad. The
owner round-trips a write/read (and an upsert overwrite) via `ArchivistMemoryRepo`; a
non-owner (capability-token) principal sees ZERO rows and cannot write (the owner WITH
CHECK blocks it). The memory handlers' happy path is exercised through the same repo.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.archivisttools import build_archivist_memory_handlers
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.archivist import ArchivistMemoryRepo
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


async def test_owner_write_read_and_overwrite_roundtrips(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = ArchivistMemoryRepo()

    async with scoped_session(maker, owner) as session:
        assert await repo.read(session, owner.principal_id) == ""  # empty before any write
        await repo.write(session, owner.principal_id, "taxonomy: Finance/Chase")

    async with scoped_session(maker, owner) as session:
        assert await repo.read(session, owner.principal_id) == "taxonomy: Finance/Chase"
        await repo.write(session, owner.principal_id, "taxonomy: Finance/Chase, Travel")  # upsert

    async with scoped_session(maker, owner) as session:
        assert await repo.read(session, owner.principal_id) == "taxonomy: Finance/Chase, Travel"


async def test_handlers_roundtrip_under_owner_scope(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    handlers = build_archivist_memory_handlers(maker)
    ctx = ToolContext(session=owner, scopes=())

    saved = await handlers["archivist_memory_write"]({"content": "rule: newsletters→Promo"}, ctx)
    assert "saved" in saved.lower()
    assert await handlers["archivist_memory_read"]({}, ctx) == "rule: newsletters→Promo"


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = ArchivistMemoryRepo()

    async with scoped_session(maker, owner) as session:
        await repo.write(session, owner.principal_id, "owner-only secret")

    # A non-owner principal sees zero rows — RLS hides the owner's scratchpad entirely.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.archivist_memory"))).scalar()
    assert count == 0

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await repo.write(session, "sneaky", "should be blocked")

    # The owner's row is intact and unchanged.
    async with scoped_session(maker, owner) as session:
        assert await repo.read(session, owner.principal_id) == "owner-only secret"
