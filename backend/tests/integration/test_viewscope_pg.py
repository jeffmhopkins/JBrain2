"""View-scope membership repo against real Postgres (JBrain360 M2b).

Proves `SqlViewScopeRepo.may_view` — the live-path's membership check, wrapping the
SECURITY DEFINER `app.viewer_may_see` — answers true only for subjects that share a
family group, deny-by-default.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.locations.viewscope import SqlViewScopeRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _subject(maker: async_sessionmaker, name: str) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
    return sid


async def _group(maker: async_sessionmaker, members: list[str]) -> None:
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES ('fam') RETURNING id")
            )
        ).scalar()
        for sid in members:
            await session.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": sid},
            )


async def test_may_view_true_for_co_members_false_otherwise(maker: async_sessionmaker) -> None:
    repo = SqlViewScopeRepo(maker)
    a = await _subject(maker, "A")
    b = await _subject(maker, "B")
    c = await _subject(maker, "C")
    await _group(maker, [a, b])

    assert await repo.may_view(a, b) is True
    assert await repo.may_view(b, a) is True  # co-membership is symmetric
    assert await repo.may_view(a, c) is False  # C shares no group
    assert await repo.may_view("", b) is False  # empty viewer fails closed
