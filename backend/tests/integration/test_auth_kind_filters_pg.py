"""The login and session-cookie lookups are kind-filtered in SQL (security plan S1.2, F4).

The in-memory fake mirrors these filters; this pins the real queries: login mints a
session for the owner key alone, and a session row belonging to a kind that never holds
a cookie (capability token, external LLM) no longer authenticates.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import InvalidCredentials
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


async def test_login_refuses_a_device_key(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    device_key = keys.generate_owner_key()
    await repo.create_principal("device_key", keys.hash_key(device_key), "phone")
    with pytest.raises(InvalidCredentials):
        await auth_service.login(repo, device_key, "x")
    # The same key still works where it belongs.
    assert await auth_service.authenticate_device(repo, device_key) is not None


async def test_login_still_admits_the_owner(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    key = await auth_service.rotate_owner_key(repo)
    token = await auth_service.login(repo, key, "pixel")
    principal = await auth_service.authenticate(repo, token, device=None)
    assert principal is not None and principal.kind == "owner"


@pytest.mark.parametrize("kind", ["capability_token", "external_llm"])
async def test_a_leftover_session_of_a_non_holding_kind_is_dead(
    maker: async_sessionmaker, kind: str
) -> None:
    repo = SqlAuthRepo(maker)
    if kind == "capability_token":
        _, record = await auth_service.mint_capability(repo, "leftover", ttl_hours=24)
        principal_id = record.id
    else:
        _, ext = await auth_service.mint_external_llm(repo, "leftover", None)
        principal_id = ext.id
    # As the old unfiltered login would have minted it.
    token = keys.generate_session_token()
    await repo.create_session(principal_id, keys.hash_token(token), "old")
    assert await auth_service.authenticate(repo, token, device=None) is None


async def test_a_member_device_session_still_authenticates(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    device_key = keys.generate_owner_key()
    await repo.create_principal("device_key", keys.hash_key(device_key), "phone")
    token = await auth_service.mint_dashboard_session(repo, device_key)
    assert token is not None
    principal = await auth_service.authenticate(repo, token, device=None)
    assert principal is not None and principal.kind == "device_key"
