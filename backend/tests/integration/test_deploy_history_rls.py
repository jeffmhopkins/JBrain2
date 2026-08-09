"""Migration 0160 against real Postgres: `deploy_history` is owner-only (CLAUDE.md rule 3),
and the `record_if_changed` contract holds.

The mandatory per-new-table RLS isolation test, plus the recorder's dedupe: the owner
appends one row per NEW git_sha (recording the same version again is a no-op), `recent`
returns the timeline newest-first, and a non-owner (capability-token) principal sees ZERO
rows and cannot write.
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
from jbrain.models.telemetry import DeployHistoryRepo
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


async def test_owner_records_dedupes_and_reads_newest_first(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = DeployHistoryRepo()

    async with scoped_session(maker, owner) as session:
        first = await repo.record_if_changed(
            session, git_sha="aaa", git_describe="v1-gaaa", build_time="t1"
        )
        assert first is not None
        # The same version again → no new row (a plain restart onto the same image is a no-op).
        again = await repo.record_if_changed(
            session, git_sha="aaa", git_describe="v1-gaaa", build_time="t1"
        )
        assert again is None

    # A genuinely new build (a separate transaction, so a strictly later deployed_at).
    async with scoped_session(maker, owner) as session:
        second = await repo.record_if_changed(
            session, git_sha="bbb", git_describe="v2-gbbb", build_time="t2"
        )
        assert second is not None

    async with scoped_session(maker, owner) as session:
        rows = await repo.recent(session, limit=10)
    assert [r.git_sha for r in rows] == ["bbb", "aaa"]  # newest first — the version timeline
    assert rows[0].git_describe == "v2-gbbb" and rows[0].build_time == "t2"


async def test_non_owner_sees_nothing_and_cannot_write(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    repo = DeployHistoryRepo()

    async with scoped_session(maker, owner) as session:
        await repo.record_if_changed(session, git_sha="aaa", git_describe="v1", build_time="t1")

    # A non-owner principal sees zero rows — RLS hides the whole table.
    async with scoped_session(maker, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.deploy_history"))).scalar()
    assert count == 0

    # …and cannot write: the owner WITH CHECK rejects a non-owner insert.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, NON_OWNER) as session:
            await repo.record_if_changed(session, git_sha="ccc", git_describe="v3", build_time="t3")
