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
    """Rotate the owner key and return a context for the principal it minted — the ACTIVE
    one, so a second call (a rotation mid-test) is the new owner, not a superseded row."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
            )
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _clear_memory(maker: async_sessionmaker, owner: SessionContext) -> None:
    """`database_url` is module-scoped, so rows outlive a test. A test that asserts on the
    scratchpad as a WHOLE (an empty read, a row count) has to start from a known table."""
    async with scoped_session(maker, owner) as session:
        await session.execute(text("DELETE FROM app.archivist_memory"))


async def test_owner_write_read_and_overwrite_roundtrips(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = ArchivistMemoryRepo()
    await _clear_memory(maker, owner)

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


async def test_write_receipt_reports_what_it_replaced(maker: async_sessionmaker) -> None:
    """The receipt is the model's only ground truth about a replace it just performed —
    the archivist apologized for destroying notes its read had returned as empty."""
    owner = await _owner(maker)
    handlers = build_archivist_memory_handlers(maker)
    ctx = ToolContext(session=owner, scopes=())
    await _clear_memory(maker, owner)

    first = await handlers["archivist_memory_write"]({"content": "taxonomy: Finance"}, ctx)
    assert "nothing was stored before" in first

    second = await handlers["archivist_memory_write"]({"content": "oops"}, ctx)
    assert "GONE" in second and "17 chars → 4" in second


async def test_memory_survives_an_owner_key_rotation(maker: async_sessionmaker) -> None:
    """A rotation revokes the owner principal and mints a new one. The scratchpad is keyed
    by principal id, so without the carry-forward the archivist would start blind with its
    real notes one row over — which is how the owner's box lost its June taxonomy."""
    old_owner = await _owner(maker)
    handlers = build_archivist_memory_handlers(maker)
    old_ctx = ToolContext(session=old_owner, scopes=())
    await _clear_memory(maker, old_owner)
    await handlers["archivist_memory_write"]({"content": "taxonomy: Finance/Chase"}, old_ctx)

    new_owner = await _owner(maker)  # the rotation
    assert new_owner.principal_id != old_owner.principal_id
    new_ctx = ToolContext(session=new_owner, scopes=())

    assert await handlers["archivist_memory_read"]({}, new_ctx) == "taxonomy: Finance/Chase"

    # The next write files the document under the current principal, so the carry-forward
    # stops firing: the new owner reads its own row from here on.
    await handlers["archivist_memory_write"](
        {"content": "taxonomy: Finance/Chase, Travel"}, new_ctx
    )
    async with scoped_session(maker, new_owner) as session:
        assert (
            await ArchivistMemoryRepo().read(session, new_owner.principal_id)
            == "taxonomy: Finance/Chase, Travel"
        )
        rows = (await session.execute(text("SELECT count(*) FROM app.archivist_memory"))).scalar()
    assert rows == 2  # the superseded row is left alone, not rewritten


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
