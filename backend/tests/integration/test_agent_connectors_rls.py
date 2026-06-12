"""Migration 0019 against real Postgres: the connector cache/log firewall
(CLAUDE.md rule 3, ASSISTANT.md invariant #9). Cached reference data is
domain-scoped — the location cache is location-scoped — and owner-only."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
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


async def _cache(maker: async_sessionmaker, connector: str, code: str, tag: str) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.connector_cache (connector, input_hash, result, domain_code)"
                " VALUES (:c, :h, '{}', :code)"
            ),
            {"c": connector, "h": f"{tag}-{code}", "code": code},
        )


async def test_connector_cache_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health", "location"):
        await _cache(maker, "lookup_medication", code, tag)
    like = {"t": f"{tag}-%"}

    # A health-only session reads only the health-domain cache.
    health = read_context(str(uuid.uuid4()), ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.connector_cache WHERE input_hash LIKE :t"),
                    like,
                )
            ).scalars()
        )
    assert rows == ["health"]

    # The unnarrowed owner reads all; a non-owner reads none (#8/#9).
    async with scoped_session(maker, OWNER) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.connector_cache WHERE input_hash LIKE :t"), like
            )
        ).scalar() == 3
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.connector_cache WHERE input_hash LIKE :t"), like
            )
        ).scalar() == 0


async def test_narrowed_session_cannot_cache_outside_scope(maker: async_sessionmaker) -> None:
    health = read_context(str(uuid.uuid4()), ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.connector_cache (connector, input_hash, result, domain_code)"
                    " VALUES ('geocode', 'x', '{}', 'location')"
                )
            )
