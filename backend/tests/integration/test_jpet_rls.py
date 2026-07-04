"""Migration 0123 against real Postgres: the JPet firewall (CLAUDE.md rule 3).

The kids' pet is owner-only and single-domain — its `pet_state` row lives in one
in-scope domain, a non-owner principal (a kid/family device session) sees none, and
a narrowed session cannot read or create a pet in a domain it lacks. So the pet can
never surface a health/finance/location fact: its row isn't even visible out of
scope. Mirrors test_lists_rls.
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


async def _make_pet(maker: async_sessionmaker, pid: str, code: str, name: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        rid = (
            await session.execute(
                text(
                    "INSERT INTO app.pet_state (principal_id, domain_code, name)"
                    " VALUES (:pid, :code, :name) RETURNING id"
                ),
                {"pid": pid, "code": code, "name": name},
            )
        ).scalar()
    return str(rid)


async def test_pet_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health", "finance"):
        await _make_pet(maker, pid, code, f"{tag}-{code}")
    like = {"t": f"{tag}%"}

    # A health-only session sees only the health pet.
    health = read_context(pid, ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.pet_state WHERE name LIKE :t"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # The unnarrowed owner sees all three; a non-owner (a kid device) sees none.
    async with scoped_session(maker, OWNER) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.pet_state WHERE name LIKE :t"), like
            )
        ).scalar() == 3
    kid = SessionContext(principal_kind="device_key", domain_scopes=("general",))
    async with scoped_session(maker, kid) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.pet_state WHERE name LIKE :t"), like
            )
        ).scalar() == 0


async def test_pet_memory_is_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health"):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(
                text(
                    "INSERT INTO app.pet_memory (principal_id, domain_code, kind, body)"
                    " VALUES (:pid, :code, 'said', :body)"
                ),
                {"pid": pid, "code": code, "body": f"{tag}-{code}"},
            )
    like = {"t": f"{tag}%"}
    count = text("SELECT count(*) FROM app.pet_memory WHERE body LIKE :t")
    # A health-only session sees only its own domain's memory; a kid device sees none.
    async with scoped_session(maker, read_context(pid, ("health",))) as session:
        assert (await session.execute(count, like)).scalar() == 1
    kid = SessionContext(principal_kind="device_key", domain_scopes=("general",))
    async with scoped_session(maker, kid) as session:
        assert (await session.execute(count, like)).scalar() == 0


async def test_narrowed_owner_cannot_create_pet_outside_scope(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    health = read_context(pid, ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.pet_state (principal_id, domain_code, name)"
                    " VALUES (:pid, 'finance', 'sneaky')"
                ),
                {"pid": pid},
            )
