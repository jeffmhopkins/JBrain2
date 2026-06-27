"""External-LLM sessions against real Postgres (migration 0104).

Proves the SQL the in-memory fake stands in for: a minted session resolves kind-
isolated, the on/off toggle (suspended_at) and revocation fail auth closed, expiry is
honoured, and usage counters accumulate atomically.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_mint_resolves_kind_isolated(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    key, record = await auth_service.mint_external_llm(repo, "Remote", ttl_hours=None)
    info = await repo.find_active_external_llm_by_key_hash(keys.hash_token(key))
    assert info is not None and info.id == record.id and info.kind == "external_llm"
    # Kind isolation: an external secret never resolves on other credential paths.
    assert await auth_service.authenticate_device(repo, key) is None
    assert await auth_service.authenticate_capability(repo, key) is None
    owner_key = await auth_service.rotate_owner_key(repo)
    assert await auth_service.authenticate_external_llm(repo, owner_key) is None


async def test_toggle_revoke_and_expiry_fail_closed(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    key, record = await auth_service.mint_external_llm(repo, "x", ttl_hours=None)
    assert await repo.find_active_external_llm_by_key_hash(keys.hash_token(key)) is not None

    # OFF (suspend) fails auth; ON restores it.
    assert await repo.set_external_llm_enabled(record.id, False) is True
    assert await repo.find_active_external_llm_by_key_hash(keys.hash_token(key)) is None
    assert await repo.set_external_llm_enabled(record.id, True) is True
    assert await repo.find_active_external_llm_by_key_hash(keys.hash_token(key)) is not None

    # Revoke is permanent.
    assert await repo.revoke_external_llm(record.id) is True
    assert await repo.find_active_external_llm_by_key_hash(keys.hash_token(key)) is None
    assert await repo.revoke_external_llm(record.id) is False  # second revoke: no row

    # A lapsed session never authenticates.
    lapsed_key, _ = await auth_service.mint_external_llm(repo, "y", ttl_hours=0.25)
    async with scoped_session(maker, SessionContext(auth_context="bootstrap")) as session:
        await session.execute(
            text(
                "UPDATE app.principals SET expires_at = now() - interval '1 hour' "
                "WHERE key_hash = :h"
            ),
            {"h": keys.hash_token(lapsed_key)},
        )
    assert await repo.find_active_external_llm_by_key_hash(keys.hash_token(lapsed_key)) is None


async def test_usage_counters_accumulate(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    _, record = await auth_service.mint_external_llm(repo, "meter", ttl_hours=None)
    await repo.add_external_usage(record.id, 100, 200)
    await repo.add_external_usage(record.id, 5, 7)
    row = {s.id: s for s in await repo.list_external_llm()}[record.id]
    assert (row.in_tokens, row.out_tokens, row.requests) == (105, 207, 2)
    assert row.last_used_at is not None
