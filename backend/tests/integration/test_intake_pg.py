"""Mint / redeem / revoke flows for guided-intake links against real Postgres.

Covers the two run accounting ceilings, both redeem branches (bind-on-first vs.
open), atomic concurrent redeem, TTL fail-closed, and the revoke cascade that kills
in-flight session cookies (GUIDED_INTAKE_PLAN.md §5/§7).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.intake import service
from jbrain.intake.repo import SqlIntakeRepo
from jbrain.intake.service import IntakeLinkConfig
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


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    await auth_service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _subject(maker: async_sessionmaker, ctx: SessionContext) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, 'S', 'person')"),
            {"i": sid},
        )
    return sid


def _config(subject_id: str, **over: object) -> IntakeLinkConfig:
    base: dict = dict(
        subject_id=subject_id,
        domain_code="general",
        label="intake",
        persona_brief="",
        fields_brief="collect a phone number",
        opening_blurb="hi",
        max_runs=5,
        max_opens=5,
        bind_on_first=False,
        ttl_hours=24.0,
    )
    base.update(over)
    return IntakeLinkConfig(**base)  # type: ignore[arg-type]


async def _mint(maker: async_sessionmaker, ctx: SessionContext, **over: object) -> str:
    sid = await _subject(maker, ctx)
    secret, _ = await service.mint_intake_link(SqlIntakeRepo(maker), ctx, _config(sid, **over))
    return secret


async def test_mint_stores_only_a_hash_and_redeem_resolves(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    sid = await _subject(maker, ctx)
    repo = SqlIntakeRepo(maker)
    secret, record = await service.mint_intake_link(repo, ctx, _config(sid))

    # Only a hash is stored — never the plaintext (#14).
    async with scoped_session(maker, ctx) as session:
        stored = (
            await session.execute(
                text("SELECT secret_hash FROM app.intake_links WHERE id = :i"), {"i": record.id}
            )
        ).scalar()
    assert stored == keys.hash_token(secret) and secret not in str(stored)

    claim = await repo.claim(
        secret_hash=keys.hash_token(secret), principal_key_hash=keys.hash_token("k"), label="x"
    )
    assert claim is not None and claim.link_id == record.id


async def test_open_branch_respects_max_opens(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    secret = await _mint(maker, ctx, bind_on_first=False, max_opens=2, max_runs=10)
    repo = SqlIntakeRepo(maker)

    async def claim() -> object | None:
        return await repo.claim(
            secret_hash=keys.hash_token(secret),
            principal_key_hash=keys.hash_token(uuid.uuid4().hex),
            label="x",
        )

    assert await claim() is not None
    assert await claim() is not None
    assert await claim() is None  # third redeem exceeds the opens ceiling


async def test_bind_on_first_is_single_use(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    # max_opens is generous, but bind-on-first caps effective opens at 1 (one person).
    secret = await _mint(maker, ctx, bind_on_first=True, max_opens=4, max_runs=10)
    repo = SqlIntakeRepo(maker)
    first = await repo.claim(
        secret_hash=keys.hash_token(secret), principal_key_hash=keys.hash_token("a"), label="x"
    )
    second = await repo.claim(
        secret_hash=keys.hash_token(secret), principal_key_hash=keys.hash_token("b"), label="x"
    )
    assert first is not None and second is None


async def test_runs_ceiling_kills_redeem(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    sid = await _subject(maker, ctx)
    repo = SqlIntakeRepo(maker)
    secret, record = await service.mint_intake_link(
        repo, ctx, _config(sid, max_runs=1, max_opens=5)
    )
    # Simulate a submission having burned the single run (W3 does this on capture).
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("UPDATE app.intake_links SET runs_used = 1 WHERE id = :i"), {"i": record.id}
        )
    assert (
        await repo.claim(
            secret_hash=keys.hash_token(secret), principal_key_hash=keys.hash_token("a"), label="x"
        )
        is None
    )


async def test_expired_link_fails_closed(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    sid = await _subject(maker, ctx)
    repo = SqlIntakeRepo(maker)
    secret, _ = await service.mint_intake_link(repo, ctx, _config(sid, ttl_hours=-1.0))
    assert (
        await repo.claim(
            secret_hash=keys.hash_token(secret), principal_key_hash=keys.hash_token("a"), label="x"
        )
        is None
    )


async def test_concurrent_redeem_never_exceeds_cap(maker: async_sessionmaker) -> None:
    """The atomic UPDATE gate: 8 concurrent redeems of a cap-3 link yield exactly 3."""
    ctx = await _owner_ctx(maker)
    secret = await _mint(maker, ctx, bind_on_first=False, max_opens=3, max_runs=20)
    repo = SqlIntakeRepo(maker)
    results = await asyncio.gather(
        *(
            repo.claim(
                secret_hash=keys.hash_token(secret),
                principal_key_hash=keys.hash_token(uuid.uuid4().hex),
                label="x",
            )
            for _ in range(8)
        )
    )
    assert sum(1 for r in results if r is not None) == 3


async def test_redeem_mints_cookie_capped_to_link_expiry(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    sid = await _subject(maker, ctx)
    intake_repo = SqlIntakeRepo(maker)
    auth_repo = SqlAuthRepo(maker)
    secret, record = await service.mint_intake_link(intake_repo, ctx, _config(sid))

    result = await service.redeem_intake_link(intake_repo, auth_repo, secret)
    assert result is not None
    # The cookie authenticates as the non-owner intake principal.
    who = await auth_service.authenticate(auth_repo, result.cookie_token)
    assert who is not None and who.kind == "intake_link"
    # The per-session principal carries the link's expiry, so the cookie dies at TTL.
    async with scoped_session(maker, ctx) as session:
        pexp = (
            await session.execute(
                text("SELECT expires_at FROM app.principals WHERE id = :p"), {"p": who.id}
            )
        ).scalar()
    assert pexp == record.expires_at


async def test_revoke_cascades_to_session_cookies(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    sid = await _subject(maker, ctx)
    intake_repo = SqlIntakeRepo(maker)
    auth_repo = SqlAuthRepo(maker)
    secret, record = await service.mint_intake_link(intake_repo, ctx, _config(sid))
    result = await service.redeem_intake_link(intake_repo, auth_repo, secret)
    assert result is not None
    assert await auth_service.authenticate(auth_repo, result.cookie_token) is not None

    # Revoking the link kills the in-flight cookie AND blocks new redeems.
    assert await service.revoke_intake_link(intake_repo, ctx, record.id) is True
    assert await auth_service.authenticate(auth_repo, result.cookie_token) is None
    assert await service.redeem_intake_link(intake_repo, auth_repo, secret) is None
    # A second revoke is a no-op.
    assert await service.revoke_intake_link(intake_repo, ctx, record.id) is False
