"""Saved places on the map against real Postgres (GUI gate `docs/mocks/minecraft-map-layers/`,
places A, owner 2026-10-13; `app.mc_places`, migration 0228).

The contract:
- every player's places in one world folder, any dimension or one, coloured by the
  player's newest gamertag from the play sessions, not the row's;
- a place says when it was put where it is (`updated_at`) and whether it was moved;
- a No-Spawn Beacon covers it by the pack's own chunk rule;
- a gate leads *here* only when its aim is this place's block, height and all (the
  review's X1; a place under another shares its column); a gate
  named for it but aimed where it was is `gates_moved`;
- a map-share visitor reads no place: the table has no share policy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import mc_share_context, scoped_session
from jbrain.minecraft import places
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_places", "mc_world_beacons", "mc_world_gates", "mc_player_sessions")
FOX = "2535411"
T0 = datetime(2026, 10, 11, 21, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


async def _seed(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as s:
        for name, x, y, z, dim, made, moved in (
            ("Home", 70, 71, 12, "overworld", T0, T0),
            ("Tree farm", -160, 68, -140, "overworld", T0, T0),
            ("Nether base", -20, 72, 40, "nether", T0, T1),
            ("Bakery", 40, 70, -56, "overworld", T0, T1),
            # Dug under Tree farm: the same column, another block.
            ("Mine", -160, 12, -140, "overworld", T0, T0),
        ):
            who = (FOX, "BlockyFox") if name != "Bakery" else ("name:Mira_P", "Mira_P")
            await s.execute(
                text(
                    "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim,"
                    " created_at, updated_at)"
                    " VALUES (:k, :g, 'castle', :n, :x, :y, :z, :d, :c, :u)"
                ),
                {
                    "k": who[0],
                    "g": who[1],
                    "n": name,
                    "x": x,
                    "y": y,
                    "z": z,
                    "d": dim,
                    "c": made,
                    "u": moved,
                },
            )
        # Another world's place never shows.
        await s.execute(
            text(
                "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                " VALUES (:k, 'BlockyFox', 'other', 'Home', 0, 64, 0, 'overworld')"
            ),
            {"k": FOX},
        )
        # BlockyFox renamed himself: the newest session's gamertag is the one shown.
        for i, (tag, at) in enumerate((("BlockyFox", T0), ("Blocky_Fox", T1))):
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_sessions (xuid, gamertag, boot_id,"
                    " start_event_id, started_at, last_seen_at)"
                    " VALUES (:k, :g, 'b', :i, :t, :t)"
                ),
                {"k": FOX, "g": tag, "i": i, "t": at},
            )
        await s.execute(
            text(
                "INSERT INTO app.mc_world_beacons (world, dim, x, y, z, placed_by, seen_at)"
                " VALUES ('castle', 'overworld', 90, 74, -10, 'BlockyFox', :t),"
                " ('castle', 'nether', 70, 70, 12, '', :t)"
            ),
            {"t": T0},
        )
        gates = (
            # Aimed at Tree farm's block (the pack aims at the place's own Y): it leads there.
            (1, "Tree farm", "overworld", -160, 68, -140, "BlockyFox"),
            # Aimed at Nether base before it was moved: it leads where it was.
            (3, "Nether base", "nether", -40, 72, 40, "Blocky_Fox"),
            # Another planter's gate that happens to be named Home: not Home's.
            (4, "Home", "overworld", 0, 70, 0, "Mira_P"),
        )
        for gid, place, dim, x, y, z, planter in gates:
            await s.execute(
                text(
                    "INSERT INTO app.mc_world_gates (world, gate_id, dim, x, y, z, axis,"
                    " planter, state, target_place, target_dim, target_x, target_y,"
                    " target_z, seen_at)"
                    " VALUES ('castle', :id, 'overworld', 28, 71, 14, 'x', :p, 'open', :tp,"
                    " :td, :tx, :ty, :tz, :t)"
                ),
                {
                    "id": gid,
                    "p": planter,
                    "tp": place,
                    "td": dim,
                    "tx": x,
                    "ty": y,
                    "tz": z,
                    "t": T0,
                },
            )
        await s.execute(
            text(
                "INSERT INTO app.mc_world_gates (world, gate_id, dim, x, y, z, axis, planter,"
                " state, seen_at) VALUES ('castle', 2, 'overworld', -70, 66, 60, 'z',"
                " 'Mira_P', 'dormant', :t)"
            ),
            {"t": T0},
        )


async def test_every_players_places_with_whose_when_and_what_covers_them(maker) -> None:
    await _seed(maker)
    got = await places.for_world(maker, "castle")
    by = {p["name"]: p for p in got["places"]}
    assert list(by) == ["Home", "Mine", "Nether base", "Tree farm", "Bakery"]  # player, name
    home = by["Home"]
    assert home["gamertag"] == "Blocky_Fox" and home["xuid"] == FOX
    # A player the server hasn't seen keeps the row's own gamertag.
    assert by["Bakery"]["gamertag"] == "Mira_P"
    assert home["saved_at"] == T0.timestamp() and home["moved"] is False
    assert by["Nether base"]["moved"] is True
    assert by["Nether base"]["saved_at"] == T1.timestamp()
    assert got["as_of"] == T1.timestamp()
    # Home's chunk (4, 0) is within one of the beacon's (5, -1); the Nether beacon at the
    # same numbers is in another dimension.
    assert home["covered"] == [{"x": 90, "y": 74, "z": -10, "placed_by": "BlockyFox"}]
    assert by["Tree farm"]["covered"] == []
    assert by["Bakery"]["covered"] == []


async def test_a_gate_leads_to_a_place_by_its_block_not_its_name(maker) -> None:
    await _seed(maker)
    by = {p["name"]: p for p in (await places.for_world(maker, "castle"))["places"]}
    assert (by["Tree farm"]["gates"], by["Tree farm"]["gates_moved"]) == ([1], [])
    assert (by["Nether base"]["gates"], by["Nether base"]["gates_moved"]) == ([], [3])
    # Gate 4 is Mira's, named "Home" and aimed elsewhere: it isn't BlockyFox's Home's.
    assert (by["Home"]["gates"], by["Home"]["gates_moved"]) == ([], [])
    # The mine under Tree farm shares its X and Z, not its block.
    assert (by["Mine"]["gates"], by["Mine"]["gates_moved"]) == ([], [])


async def test_one_dimension_and_a_world_with_none(maker) -> None:
    await _seed(maker)
    nether = await places.for_world(maker, "castle", "nether")
    assert [p["name"] for p in nether["places"]] == ["Nether base"]
    assert await places.for_world(maker, "nowhere") == {"places": [], "as_of": None}


async def test_a_map_share_visitor_reads_no_place(maker) -> None:
    await _seed(maker)
    got = await places.for_world(maker, "castle", ctx=mc_share_context("any"))
    assert got == {"places": [], "as_of": None}
