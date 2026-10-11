"""Migration 0230 against real Postgres: a map share link sees its own world, in RLS.

The keystone. The travel log is owner-only; a public visitor runs under
`mc_share_context`, a non-owner, empty-scope principal pinned to one link id. The `*_share`
policies are its only grants: pinned to a link it reads every player's track, fog, events
and sessions in that link's world and nothing from any other world or table; a revoked
link or an empty pin reads nothing; and it can write nowhere. The readers the public routes
call (`latest_positions`, `timeline`) give the same answers under it as the owner gets.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, mc_share_context, scoped_session
from jbrain.minecraft import shares, timeline, travel
from tests.conftest import docker_available
from tests.integration.test_mc_timeline_pg import FOX, MIRA, T0, Seed
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = (
    "mc_share_links",
    "mc_player_track",
    "mc_player_explored",
    "mc_player_events",
    "mc_player_sessions",
)
WORLD_TABLES = ("mc_player_track", "mc_player_explored", "mc_player_events")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    # The link's principal_id FKs to app.principals: the owner ctx carries a real one.
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _seed_two_worlds(maker: async_sessionmaker) -> None:
    seed = Seed()
    seed.session("castle", FOX, T0, T0 + 600)
    seed.walk("castle", FOX, T0, 10)
    seed.session("castle", MIRA, T0 + 100, None)
    seed.walk("castle", MIRA, T0 + 100, 5, z=50.0)
    seed.chunk("castle", FOX, 0, 0, T0, "overworld")
    seed.event("castle", FOX, "death", T0 + 300, dim="overworld", x=1.0, y=64.0, z=1.0)
    seed.session("other", FOX, T0 + 1_000, T0 + 2_000)
    seed.walk("other", FOX, T0 + 1_000, 10, x0=9_000.0)
    seed.chunk("other", FOX, 500, 500, T0 + 1_000, "overworld")
    await seed.into(maker)


async def _mint(maker: async_sessionmaker, owner: SessionContext, world: str = "castle"):
    return await shares.mint_share(
        maker, owner, slot="slot2", world=world, xuid=FOX, gamertag="BlockyFox"
    )


async def _worlds_seen(maker: async_sessionmaker, link_id: str, table: str) -> set[str]:
    async with scoped_session(maker, mc_share_context(link_id)) as s:
        rows = (await s.execute(text(f"SELECT DISTINCT world FROM app.{table}"))).all()
    return {r.world for r in rows}


async def _sessions_seen(maker: async_sessionmaker, link_id: str) -> int:
    async with scoped_session(maker, mc_share_context(link_id)) as s:
        return (await s.execute(text("SELECT count(*) FROM app.mc_player_sessions"))).scalar_one()


async def test_a_link_reads_every_player_in_its_world_and_no_other(maker) -> None:
    owner = await _owner(maker)
    await _seed_two_worlds(maker)
    secret, link = await _mint(maker, owner)

    resolved = await shares.resolve_share(maker, secret)
    assert resolved is not None and resolved.id == link.id
    assert (resolved.slot, resolved.world, resolved.xuid) == ("slot2", "castle", FOX)
    for table in WORLD_TABLES:
        assert await _worlds_seen(maker, link.id, table) == {"castle"}, table
    # Both of castle's sessions (either player), not the one filed under the other world.
    assert await _sessions_seen(maker, link.id) == 2


async def test_the_readers_answer_the_same_under_the_share(maker) -> None:
    owner = await _owner(maker)
    await _seed_two_worlds(maker)
    _, link = await _mint(maker, owner)
    ctx = mc_share_context(link.id)

    mine = await travel.latest_positions(maker, "castle")
    theirs = await travel.latest_positions(maker, "castle", ctx)
    assert {p["xuid"] for p in theirs} == {FOX, MIRA}
    assert theirs == mine

    span = (T0 - 60, T0 + 3_000)
    parts = frozenset({"trails", "events", "sessions", "explored"})
    want = await timeline.timeline(maker, "castle", *span, include=parts)
    got = await timeline.timeline(maker, "castle", *span, include=parts, ctx=ctx)
    for key in ("trails", "events", "sessions", "explored"):
        assert got[key] == want[key], key
    # Asking the share for another world's log reads nothing, whatever the caller passes.
    other = await timeline.timeline(maker, "other", *span, include=parts, ctx=ctx)
    assert other["trails"] == [] and other["sessions"] == []
    assert other["explored"]["chunks"] == []
    assert await travel.latest_positions(maker, "other", ctx) == []


async def test_a_revoked_link_or_an_empty_pin_reads_nothing(maker) -> None:
    owner = await _owner(maker)
    await _seed_two_worlds(maker)
    secret, link = await _mint(maker, owner)

    for table in WORLD_TABLES:
        assert await _worlds_seen(maker, "", table) == set()
    assert await _sessions_seen(maker, "") == 0

    assert await shares.revoke_share(maker, owner, link.id) is True
    assert await shares.resolve_share(maker, secret) is None
    for table in WORLD_TABLES:
        assert await _worlds_seen(maker, link.id, table) == set()
    assert await _sessions_seen(maker, link.id) == 0
    assert await shares.revoke_share(maker, owner, link.id) is False
    assert await shares.revoke_share(maker, owner, "not-a-uuid") is False


async def test_the_share_leaks_nothing_else_and_writes_nowhere(maker) -> None:
    owner = await _owner(maker)
    await _seed_two_worlds(maker)
    _, link = await _mint(maker, owner)
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'health', 'BP 118/76')"
            ),
            {"id": str(uuid.uuid4()), "cid": f"mc-{uuid.uuid4().hex[:12]}"},
        )
    async with scoped_session(maker, mc_share_context(link.id)) as s:
        for table in ("app.notes", "app.mc_places", "app.principals"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0, f"the share must not read {table}"

    with pytest.raises(DBAPIError):
        async with scoped_session(maker, mc_share_context(link.id)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_events (world, xuid, gamertag, boot_id,"
                    " event_id, at, kind) VALUES ('castle', 'x', 'x', 'b', 1, now(), 'join')"
                )
            )
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, mc_share_context(link.id)) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_share_links"
                    " (token_hash, slot, world, xuid, gamertag, principal_id)"
                    " VALUES (:h, 'slot1', 'other', 'x', 'x', cast(:pid AS uuid))"
                ),
                {"h": uuid.uuid4().hex, "pid": owner.principal_id},
            )
    # A visitor's revoke is a no-op: the update policy is the owner's.
    async with scoped_session(maker, mc_share_context(link.id)) as s:
        done = (
            await s.execute(text("UPDATE app.mc_share_links SET revoked_at = now() RETURNING id"))
        ).all()
    assert done == []


async def test_views_count_only_when_asked_and_the_list_is_the_owners(maker) -> None:
    owner = await _owner(maker)
    secret, link = await _mint(maker, owner)
    _, other = await _mint(maker, owner, world="other")

    await shares.resolve_share(maker, secret)
    await shares.resolve_share(maker, secret, count_view=True)
    listed = {s.id: s for s in await shares.list_shares(maker, owner)}
    assert set(listed) == {link.id, other.id}
    assert listed[link.id].view_count == 1 and listed[link.id].last_viewed_at is not None
    assert await shares.resolve_share(maker, "   ") is None

    await shares.revoke_share(maker, owner, other.id)
    assert [s.id for s in await shares.list_shares(maker, owner)] == [link.id]
    # Nobody but the owner lists links.
    async with scoped_session(maker, SessionContext(principal_kind="member")) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.mc_share_links"))).scalar_one() == 0
