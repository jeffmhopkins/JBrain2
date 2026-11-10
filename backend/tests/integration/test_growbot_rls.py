"""Migration 0254 against real Postgres: GrowBot's robots and tickets are owner-only.

The mandatory RLS isolation test for both new tables (CLAUDE.md rule 3), plus the repo's
security rules where only the database can prove them: a ticket redeems exactly once, even
when two redeems race, and never after its 120 s; a device key is stored only as its hash and
a rotation revokes the old one.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth.keys import hash_token
from jbrain.db.session import (
    SessionContext,
    device_context,
    jshare_context,
    scoped_session,
)
from jbrain.growbot.repo import SqlGrowbotRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SYSTEM_OWNER = SessionContext(principal_kind="owner")
# Every non-owner shape a request can arrive under: none may read or write either table.
NON_OWNERS = [
    UNSCOPED,
    SessionContext(principal_kind="capability_token", domain_scopes=("general",)),
    SessionContext(principal_kind="member", principal_id="m1", domain_scopes=("general",)),
    device_context("d1", "s1"),
    jshare_context("slug"),
    SessionContext(auth_context="login"),
]
TABLES = ("growbot_robots", "growbot_tickets")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    # The database is the module's, so each test starts from empty tables.
    async with scoped_session(made, SYSTEM_OWNER) as session:
        for table in TABLES:
            await session.execute(text(f"DELETE FROM app.{table}"))
    yield made
    await engine.dispose()


async def _count(maker: async_sessionmaker, ctx: SessionContext, table: str) -> int:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar_one()


async def test_both_tables_are_owner_only(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    await repo.put_robot(OWNER, "gb-a1")
    await repo.mint_ticket(OWNER)
    for table in TABLES:
        assert await _count(maker, OWNER, table) == 1
        assert await _count(maker, SYSTEM_OWNER, table) == 1
        for ctx in NON_OWNERS:
            assert await _count(maker, ctx, table) == 0, (table, ctx)


@pytest.mark.parametrize("ctx", NON_OWNERS)
async def test_a_non_owner_cannot_write_either_table(
    maker: async_sessionmaker, ctx: SessionContext
) -> None:
    repo = SqlGrowbotRepo(maker)
    await repo.put_robot(OWNER, "gb-a1")
    ticket = (await repo.mint_ticket(OWNER)).ticket
    with pytest.raises(DBAPIError):
        await repo.put_robot(ctx, "gb-b2")
    with pytest.raises(DBAPIError):
        await repo.mint_ticket(ctx)
    # Nothing it can see, so nothing it can spend, verify, change or delete.
    assert not await repo.redeem_ticket(ctx, ticket)
    assert not await repo.delete_robot(ctx, "gb-a1")
    async with scoped_session(maker, ctx) as session:
        changed = await session.execute(
            text("UPDATE app.growbot_robots SET key_hash = :h"), {"h": "0" * 64}
        )
        assert changed.rowcount == 0  # type: ignore[attr-defined]
    assert [r.code for r in await repo.list_robots(OWNER)] == ["gb-a1"]
    assert await repo.redeem_ticket(SYSTEM_OWNER, ticket)


async def test_a_ticket_redeems_once_and_is_stored_hashed(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    minted = await repo.mint_ticket(OWNER)
    async with scoped_session(maker, OWNER) as session:
        stored = (
            await session.execute(text("SELECT ticket_hash FROM app.growbot_tickets"))
        ).scalar_one()
    assert stored == hash_token(minted.ticket) and minted.ticket not in stored
    assert not await repo.redeem_ticket(SYSTEM_OWNER, "")
    assert not await repo.redeem_ticket(SYSTEM_OWNER, minted.ticket + "x")
    assert await repo.redeem_ticket(SYSTEM_OWNER, minted.ticket)
    assert not await repo.redeem_ticket(SYSTEM_OWNER, minted.ticket)


async def test_racing_redeems_spend_a_ticket_once(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    ticket = (await repo.mint_ticket(OWNER)).ticket
    results = await asyncio.gather(*(repo.redeem_ticket(SYSTEM_OWNER, ticket) for _ in range(8)))
    assert sorted(results) == [False] * 7 + [True]


async def test_an_expired_ticket_is_refused_and_pruned(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    old = (await repo.mint_ticket(OWNER)).ticket
    stale = (await repo.mint_ticket(OWNER)).ticket
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "UPDATE app.growbot_tickets SET expires_at = now() - interval '1 second'"
                " WHERE ticket_hash = :h"
            ),
            {"h": hash_token(old)},
        )
        await session.execute(
            text(
                "UPDATE app.growbot_tickets SET expires_at = now() - interval '2 days'"
                " WHERE ticket_hash = :h"
            ),
            {"h": hash_token(stale)},
        )
    assert not await repo.redeem_ticket(SYSTEM_OWNER, old)
    await repo.mint_ticket(OWNER)
    # The next mint drops tickets a day past their expiry, keeping the recent trail.
    assert await _count(maker, OWNER, "growbot_tickets") == 2


async def test_a_ticket_lives_120_seconds(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    minted = await repo.mint_ticket(OWNER)
    async with scoped_session(maker, OWNER) as session:
        ttl = (
            await session.execute(
                text("SELECT extract(epoch FROM expires_at - created_at) FROM app.growbot_tickets")
            )
        ).scalar_one()
    assert float(ttl) == pytest.approx(120.0, abs=1.0)
    assert minted.expires_at.tzinfo is not None


async def test_robot_keys_verify_rotate_and_forget(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    first = await repo.put_robot(OWNER, "gb-a1")
    other = await repo.put_robot(OWNER, "gb-b2")
    async with scoped_session(maker, OWNER) as session:
        hashes = set(
            (await session.execute(text("SELECT key_hash FROM app.growbot_robots"))).scalars()
        )
    assert hashes == {hash_token(first), hash_token(other)}
    assert await repo.verify_robot(SYSTEM_OWNER, "gb-a1", first)
    assert not await repo.verify_robot(SYSTEM_OWNER, "gb-a1", other)
    assert not await repo.verify_robot(SYSTEM_OWNER, "gb-a1", "")
    assert not await repo.verify_robot(SYSTEM_OWNER, "gb-zz", first)
    # A non-owner context sees no robot, so even the right key fails there.
    assert not await repo.verify_robot(UNSCOPED, "gb-a1", first)

    rotated = await repo.put_robot(OWNER, "gb-a1")
    assert not await repo.verify_robot(SYSTEM_OWNER, "gb-a1", first)
    assert await repo.verify_robot(SYSTEM_OWNER, "gb-a1", rotated)
    listed = {r.code: r for r in await repo.list_robots(OWNER)}
    assert listed["gb-a1"].rotated_at is not None and listed["gb-b2"].rotated_at is None

    assert await repo.delete_robot(OWNER, "gb-a1")
    assert not await repo.delete_robot(OWNER, "gb-a1")
    assert not await repo.verify_robot(SYSTEM_OWNER, "gb-a1", rotated)


async def test_the_schema_refuses_a_malformed_code_or_hash(maker: async_sessionmaker) -> None:
    repo = SqlGrowbotRepo(maker)
    with pytest.raises(DBAPIError):
        await repo.put_robot(OWNER, "../etc")
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(
                text("INSERT INTO app.growbot_robots (code, key_hash) VALUES ('gb-x', 'plain')")
            )
