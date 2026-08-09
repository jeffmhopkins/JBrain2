"""Migration 0163 against real Postgres: app.blocked_domains is global-read reference
data (every scope reads the 24h skip list — a search runs under whatever scope the
caller holds) but owner/system-write only — a scoped non-owner principal cannot record
or bump a blocked domain (CLAUDE.md rule 3, E2). Mirrors the app.actions RLS precedent
(0035) and the app.canonical_predicates one (0031). Also round-trips DomainSkipRepo
under SYSTEM_CTX to prove the upsert + lazy-expiry read against a live database.
"""

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.web.domain_health import DomainSkipRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A scoped capability token (non-owner): it reads global reference data but must not
# be able to record or edit a blocked domain.
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _hosts(maker: async_sessionmaker, ctx: SessionContext) -> set[str]:
    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT host FROM app.blocked_domains"))).scalars().all()
    return set(rows)


async def _owner_insert(maker: async_sessionmaker, host: str, reason: str = "paywalled") -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.blocked_domains (host, reason, last_url)"
                " VALUES (:host, :reason, :url)"
            ),
            {"host": host, "reason": reason, "url": f"https://{host}/x"},
        )


async def test_a_recorded_domain_is_globally_readable(maker: async_sessionmaker) -> None:
    """An owner-recorded block is visible to every scope — no domain firewall on the
    global skip list (the app.actions / app.canonical_predicates precedent), since a search
    runs under whatever scope the caller holds and must still see the skip list."""
    await _owner_insert(maker, "paywalled.example")
    for ctx in (OWNER, GENERAL_ONLY, HEALTH_ONLY, UNSCOPED):
        assert "paywalled.example" in await _hosts(maker, ctx)


async def test_scoped_principal_cannot_record_a_block(maker: async_sessionmaker) -> None:
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.blocked_domains (host, reason, last_url)"
                    " VALUES ('sneaky.example', 'bot_blocked', 'https://sneaky.example/x')"
                )
            )


async def test_scoped_principal_cannot_edit_a_block(maker: async_sessionmaker) -> None:
    # The write-check fails closed: a scoped UPDATE matches no owner-writable rows and
    # silently affects nothing rather than mutating the skip list.
    await _owner_insert(maker, "edit-me.example", reason="paywalled")
    async with scoped_session(maker, GENERAL_ONLY) as s:
        result = await s.execute(
            text(
                "UPDATE app.blocked_domains SET reason = 'unreadable'"
                " WHERE host = 'edit-me.example'"
            )
        )
        assert cast(CursorResult[Any], result).rowcount == 0
    async with scoped_session(maker, OWNER) as s:
        reason = (
            await s.execute(
                text("SELECT reason FROM app.blocked_domains WHERE host = 'edit-me.example'")
            )
        ).scalar_one()
    assert reason == "paywalled"  # untouched by the scoped UPDATE


async def test_owner_can_record_a_block(maker: async_sessionmaker) -> None:
    await _owner_insert(maker, "owner-block.example")
    assert "owner-block.example" in await _hosts(maker, GENERAL_ONLY)


async def test_system_repo_upserts_and_reads_active_hosts(maker: async_sessionmaker) -> None:
    """DomainSkipRepo runs under SYSTEM_CTX (owner-kind): record upserts (a repeat bumps
    hit_count, resets the 24h window) and active_hosts reads live rows. Proves the ON CONFLICT
    upsert and the lazy-expiry read against a real database, end to end through the repo."""
    repo = DomainSkipRepo(maker)
    await repo.record("Repo.Example", "bot_blocked", "https://repo.example/a")
    # The host is normalized to a bare lowercase key.
    assert "repo.example" in await repo.active_hosts()
    # A repeat block upserts the SAME row (bumps hit_count, updates reason) — never a dup.
    await repo.record("repo.example", "paywalled", "https://repo.example/b")
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    "SELECT reason, hit_count FROM app.blocked_domains WHERE host = 'repo.example'"
                )
            )
        ).one()
    assert row.reason == "paywalled" and row.hit_count == 2


async def test_expired_rows_are_not_active(maker: async_sessionmaker) -> None:
    """Lazy expiry is the source of truth: a row whose expires_at is in the past drops out of
    active_hosts without any sweep, so a stale block simply stops matching after 24h."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.blocked_domains (host, reason, expires_at)"
                " VALUES ('stale.example', 'paywalled', now() - interval '1 hour')"
            )
        )
    assert "stale.example" not in await DomainSkipRepo(maker).active_hosts()
