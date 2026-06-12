"""Migration 0022 against real Postgres: the lists firewall (CLAUDE.md rule 3).

A list is owner-only and single-domain — you cannot make a list in a domain the
session cannot read, and a non-owner principal sees and creates none (#8). Items
inherit their list's visibility.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
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


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _make_list(maker: async_sessionmaker, pid: str, code: str, title: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        lid = (
            await session.execute(
                text(
                    "INSERT INTO app.lists (principal_id, domain_code, title)"
                    " VALUES (:pid, :code, :title) RETURNING id"
                ),
                {"pid": pid, "code": code, "title": title},
            )
        ).scalar()
        await session.execute(
            text("INSERT INTO app.list_items (list_id, body) VALUES (:lid, :body)"),
            {"lid": str(lid), "body": f"{title} item"},
        )
    return str(lid)


async def test_lists_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health", "finance"):
        await _make_list(maker, pid, code, f"{tag} {code}")
    like = {"t": f"{tag}%"}

    # A health-only owner session sees only the health list.
    health = read_context(pid, ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.lists WHERE title LIKE :t"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # The unnarrowed owner sees all three; a non-owner sees none (#8).
    async with scoped_session(maker, OWNER) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.lists WHERE title LIKE :t"), like
            )
        ).scalar() == 3
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.lists WHERE title LIKE :t"), like
            )
        ).scalar() == 0


async def test_narrowed_owner_cannot_create_outside_scope(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    health = read_context(pid, ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.lists (principal_id, domain_code, title)"
                    " VALUES (:pid, 'finance', 'sneaky')"
                ),
                {"pid": pid},
            )


async def test_items_follow_their_list(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    lid = await _make_list(maker, pid, "health", f"{tag} health")
    count_items = text("SELECT count(*) FROM app.list_items WHERE list_id = :lid")

    # A general-only session can't see the health list's items; a health one can.
    async with scoped_session(maker, read_context(pid, ("general",))) as session:
        assert (await session.execute(count_items, {"lid": lid})).scalar() == 0
    async with scoped_session(maker, read_context(pid, ("health",))) as session:
        assert (await session.execute(count_items, {"lid": lid})).scalar() == 1
