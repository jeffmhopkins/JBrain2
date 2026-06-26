"""jcode share-link auth against real Postgres (migration 0099).

Proves the SQL filtering the in-memory fake stands in for: a minted share resolves
kind-isolated and session-scoped, honours expiry + revocation, and — critically — the
session cookie it's redeemed into ALSO honours the principal's expiry, so a redeemed
cookie can't outlive the share. List/revoke are session-scoped.
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

_BOOTSTRAP = SessionContext(auth_context="bootstrap")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_mint_resolves_scoped_and_kind_isolated(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    key, record = await auth_service.mint_jcode_share(repo, "sess-a", "Sarah", ttl_hours=24)

    info = await repo.find_active_jcode_share_by_key_hash(keys.hash_token(key))
    assert info is not None
    assert info.id == record.id and info.kind == "jcode_share_link"
    assert info.jcode_session_id == "sess-a"
    # last_used_at is stamped on the hit (the owner's list shows liveness).
    assert {t.id: t for t in await repo.list_jcode_shares("sess-a")}[record.id].last_used_at

    # Kind isolation: the share key never resolves on the owner / device / capability
    # paths, and an owner key never resolves as a share.
    assert await auth_service.authenticate_device(repo, key) is None
    assert await auth_service.authenticate_capability(repo, key) is None
    owner_key = await auth_service.rotate_owner_key(repo)
    assert await auth_service.redeem_jcode_share(repo, owner_key) is None


async def test_expiry_and_revocation_fail_closed(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    expired, _ = await auth_service.mint_jcode_share(repo, "s", "x", ttl_hours=-1)
    assert await repo.find_active_jcode_share_by_key_hash(keys.hash_token(expired)) is None

    live, record = await auth_service.mint_jcode_share(repo, "s", "x", ttl_hours=24)
    assert await repo.find_active_jcode_share_by_key_hash(keys.hash_token(live)) is not None
    assert await repo.revoke_jcode_share(record.id, "s") is True
    assert await repo.find_active_jcode_share_by_key_hash(keys.hash_token(live)) is None
    # A second revoke / wrong-session revoke reports no row changed.
    assert await repo.revoke_jcode_share(record.id, "s") is False


async def test_redeemed_cookie_dies_when_the_share_lapses(maker: async_sessionmaker) -> None:
    # The load-bearing security property: expiry is enforced on the SESSION cookie, not
    # just at redeem, so a redeemed cookie cannot outlive the share's box.
    repo = SqlAuthRepo(maker)
    key, record = await auth_service.mint_jcode_share(repo, "sess-a", "x", ttl_hours=24)
    redeemed = await auth_service.redeem_jcode_share(repo, key)
    assert redeemed is not None
    cookie_token, session_id = redeemed
    assert session_id == "sess-a"
    assert await auth_service.authenticate(repo, cookie_token) is not None

    # Lapse the share principal; the cookie must now fail closed.
    async with scoped_session(maker, _BOOTSTRAP) as session:
        await session.execute(
            text(
                "UPDATE app.principals SET expires_at = now() - interval '1 second' WHERE id = :i"
            ),
            {"i": record.id},
        )
    assert await auth_service.authenticate(repo, cookie_token) is None


async def test_list_and_revoke_are_session_scoped(maker: async_sessionmaker) -> None:
    # Distinct session ids per run so the assertion doesn't depend on a clean DB.
    repo = SqlAuthRepo(maker)
    _, a = await auth_service.mint_jcode_share(repo, "scope-a", "a", ttl_hours=24)
    _, b = await auth_service.mint_jcode_share(repo, "scope-b", "b", ttl_hours=24)
    ids_a = {s.id for s in await repo.list_jcode_shares("scope-a")}
    assert a.id in ids_a and b.id not in ids_a  # the list never crosses sessions
    # Revoking with the wrong session id is a no-op (defence in depth).
    assert await repo.revoke_jcode_share(a.id, "scope-b") is False
    assert await repo.revoke_jcode_share(a.id, "scope-a") is True
    assert a.id not in {s.id for s in await repo.list_jcode_shares("scope-a")}
