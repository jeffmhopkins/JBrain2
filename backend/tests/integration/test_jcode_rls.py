"""Migration 0098 against real Postgres: `jcode_sessions` is owner-only (CLAUDE.md
rule 3).

The mandatory per-new-table RLS isolation test for code mode's launcher index. The
owner round-trips upsert/list/get/touch/delete via `JcodeSessionRepo`; a non-owner
(capability-token) principal sees ZERO rows and cannot write (the owner WITH CHECK
blocks it).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jcode import JcodeSessionRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A non-owner principal: a capability token — app.is_owner() is false.
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


async def test_owner_crud_roundtrips(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = JcodeSessionRepo()

    async with scoped_session(maker, owner) as session:
        assert await repo.list(session) == []
        await repo.upsert(
            session,
            id="s1",
            repo="github.com/me/r",
            branch="main",
            work_branch="jcode/s1",
            status="ready",
        )

    async with scoped_session(maker, owner) as session:
        rows = await repo.list(session)
        assert [r.id for r in rows] == ["s1"]
        assert rows[0].repo == "github.com/me/r"
        assert rows[0].title == ""  # the launcher label defaults empty
        assert rows[0].archived is False
        await repo.touch(session, "s1", status="running")
        await repo.rename(session, "s1", "todo spike")
        await repo.set_archived(session, "s1", True)

    async with scoped_session(maker, owner) as session:
        row = await repo.get(session, "s1")
        assert row is not None
        assert row.status == "running"  # the turn status survives rename/archive
        assert row.title == "todo spike"
        assert row.archived is True
        # A turn writing status back must NOT clear the archive flag (separate columns).
        await repo.touch(session, "s1", status="ready")

    async with scoped_session(maker, owner) as session:
        row = await repo.get(session, "s1")
        assert row is not None
        assert row.archived is True
        assert row.status == "ready"
        await repo.set_archived(session, "s1", False)
        assert (await repo.get(session, "s1")).archived is False  # type: ignore[union-attr]
        await repo.delete(session, "s1")

    async with scoped_session(maker, owner) as session:
        assert await repo.get(session, "s1") is None


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = JcodeSessionRepo()

    async with scoped_session(maker, owner) as session:
        await repo.upsert(
            session, id="secret", repo="r", branch="main", work_branch="", status="ready"
        )

    # A non-owner principal sees zero rows — RLS hides the owner's sessions entirely.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.jcode_sessions"))).scalar()
    assert count == 0

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await repo.upsert(
                session, id="sneaky", repo="r", branch="main", work_branch="", status="ready"
            )

    # The owner's row is intact.
    async with scoped_session(maker, owner) as session:
        assert (await repo.get(session, "secret")) is not None
