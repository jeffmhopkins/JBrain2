"""The map's timeline queries against real Postgres (plan §M8a, `minecraft/timeline.py`).

The contract, case by case:
- a long trail is cut to the point budget, but every session, dimension and life keeps
  its first and last point, so nothing is ever drawn or interpolated across a portal,
  a log-off or a respawn;
- only the range asked for is read, and `players` narrows trails, events and fog;
- events carry their cause and killer, joins and leaves sit at the player's samples,
  and a portal trip is derived from the trail — a respawn's jump is not;
- the session list is the world's own, newest first, with an open session "on now",
  its deaths, the distance per dimension (without the respawn jump) and where it began;
- the fog gives each chunk's first-seen time up to the range's end;
- an over-long or backwards range is refused.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.minecraft import timeline
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

T0 = 1_791_640_000.0
TABLES = ("mc_player_track", "mc_player_explored", "mc_player_events", "mc_player_sessions")
FOX, MIRA = "2535411", "2535412"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


class Seed:
    """Rows the way the drains write them: a session's join event shares its boot and
    event number, which is what files the session under a world."""

    def __init__(self) -> None:
        self.eid = 0
        self.sid = 0
        self.sql: list[tuple[str, dict[str, Any]]] = []

    def _event(self, world: str, xuid: str, kind: str, at: float, **kw: Any) -> int:
        self.eid += 1
        detail = {k: kw.pop(k) for k in ("cause", "killer") if k in kw}
        self.sql.append(
            (
                "INSERT INTO app.mc_player_events (world, xuid, gamertag, boot_id, event_id,"
                " at, kind, dim, x, y, z, detail) VALUES (:w, :x, :g, 'boot', :e,"
                " to_timestamp(:at), :k, :dim, :px, :py, :pz, CAST(:d AS jsonb))",
                {
                    "w": world,
                    "x": xuid,
                    "g": "BlockyFox" if xuid == FOX else "Mira_P",
                    "e": self.eid,
                    "at": at,
                    "k": kind,
                    "dim": kw.get("dim"),
                    "px": kw.get("x"),
                    "py": kw.get("y"),
                    "pz": kw.get("z"),
                    "d": json.dumps(detail),
                },
            )
        )
        return self.eid

    def session(self, world: str, xuid: str, start: float, end: float | None) -> None:
        eid = self._event(world, xuid, "join", start)
        self.sql.append(
            (
                "INSERT INTO app.mc_player_sessions (xuid, gamertag, boot_id, start_event_id,"
                " started_at, last_seen_at, ended_at) VALUES (:x, :g, 'boot', :e,"
                " to_timestamp(:s), to_timestamp(:l), to_timestamp(:end))",
                {
                    "x": xuid,
                    "g": "BlockyFox" if xuid == FOX else "Mira_P",
                    "e": eid,
                    "s": start,
                    "l": end or start,
                    "end": end,
                },
            )
        )
        if end is not None:
            self._event(world, xuid, "leave", end)

    def event(self, world: str, xuid: str, kind: str, at: float, **kw: Any) -> None:
        self._event(world, xuid, kind, at, **kw)

    def walk(
        self,
        world: str,
        xuid: str,
        start: float,
        n: int,
        *,
        dim: str = "overworld",
        x0: float = 0.0,
        dx: float = 10.0,
        z: float = 0.0,
        every: float = 10.0,
    ) -> None:
        """`n` samples every `every` s walking east `dx` blocks a step, in one statement."""
        self.sid += 1
        self.sql.append(
            (
                "INSERT INTO app.mc_player_track (world, xuid, gamertag, boot_id, sample_id,"
                " at, dim, x, y, z) SELECT :w, :x, :g, :b, i,"
                " to_timestamp(CAST(:t0 AS float8) + i * CAST(:every AS float8)), :dim,"
                " CAST(:x0 AS float8) + i * CAST(:dx AS float8), 64, CAST(:z AS float8)"
                " FROM generate_series(0, CAST(:n AS integer) - 1) AS i",
                {
                    "w": world,
                    "x": xuid,
                    "g": "BlockyFox" if xuid == FOX else "Mira_P",
                    "b": f"boot-{self.sid}",
                    "t0": start,
                    "every": every,
                    "dim": dim,
                    "x0": x0,
                    "dx": dx,
                    "z": z,
                    "n": n,
                },
            )
        )

    def chunk(self, world: str, xuid: str, cx: int, cz: int, first: float, dim: str) -> None:
        self.sql.append(
            (
                "INSERT INTO app.mc_player_explored (world, xuid, dim, cx, cz, first_seen,"
                " last_seen) VALUES (:w, :x, :d, :cx, :cz, to_timestamp(:f), to_timestamp(:f))",
                {"w": world, "x": xuid, "d": dim, "cx": cx, "cz": cz, "f": first},
            )
        )

    async def into(self, maker: async_sessionmaker) -> None:
        async with scoped_session(maker, OWNER) as s:
            for sql, params in self.sql:
                await s.execute(text(sql), params)


async def test_a_long_trail_is_cut_to_the_budget_keeping_every_boundary(maker) -> None:
    seed = Seed()
    seed.session("castle", FOX, T0, T0 + 30_000)
    seed.walk("castle", FOX, T0, 1_500)  # overworld, east along z=0
    seed.walk("castle", FOX, T0 + 15_000, 1_000, dim="nether", x0=5.0, dx=1.0)
    seed.walk("castle", FOX, T0 + 25_000, 400, x0=15_000.0)  # back through the portal
    await seed.into(maker)

    got = await timeline.timeline(maker, "castle", T0 - 60, T0 + 30_100)

    (trail,) = got["trails"]
    assert trail["samples"] == 2_900 and trail["downsampled"] is True
    segs = trail["segments"]
    assert sum(len(g["points"]) for g in segs) <= timeline.MAX_POINTS
    assert [g["dim"] for g in segs] == ["overworld", "nether", "overworld"]
    assert len({g["session"] for g in segs}) == 1 and segs[0]["session"] is not None
    # Every segment still starts and ends on the samples where it really did.
    assert (segs[0]["points"][0][0], segs[0]["points"][-1][0]) == (T0, T0 + 14_990)
    assert (segs[1]["points"][0][0], segs[1]["points"][-1][0]) == (T0 + 15_000, T0 + 24_990)
    assert segs[2]["points"][0] == [T0 + 25_000, 15_000.0, 64.0, 0.0]
    # The portal trips are events of their own, with where they landed.
    trips = [e for e in got["events"] if e["kind"] == "dimension"]
    assert [(e["from_dim"], e["dim"], e["at"]) for e in trips] == [
        ("overworld", "nether", T0 + 15_000),
        ("nether", "overworld", T0 + 25_000),
    ]


async def test_a_respawn_breaks_the_trail_and_is_not_a_portal_trip(maker) -> None:
    seed = Seed()
    seed.session("castle", FOX, T0, T0 + 1_000)
    seed.walk("castle", FOX, T0 + 10, 20, dim="nether", x0=41.0, dx=0.0, z=-58.0)
    seed.event(
        "castle", FOX, "death", T0 + 300, dim="nether", x=41.0, y=33.0, z=-58.0,
        cause="lava", killer="",
    )  # fmt: skip
    seed.event("castle", FOX, "respawn", T0 + 312, dim="overworld", x=-140.0, y=104.0, z=90.0)
    seed.walk("castle", FOX, T0 + 315, 20, x0=-140.0, z=90.0)
    await seed.into(maker)

    got = await timeline.timeline(maker, "castle", T0, T0 + 1_000)

    segs = got["trails"][0]["segments"]
    assert [g["dim"] for g in segs] == ["nether", "overworld"]
    kinds = [e["kind"] for e in got["events"]]
    assert kinds == ["join", "death", "respawn", "leave"]  # no "dimension" for the respawn
    death = got["events"][1]
    assert (death["cause"], death["killer"], death["x"], death["dim"]) == (
        "lava",
        None,
        41.0,
        "nether",
    )
    # A join and a leave carry no place: they sit on the player's first and last sample.
    join, leave = got["events"][0], got["events"][-1]
    assert (join["x"], join["z"], join["dim"]) == (41.0, -58.0, "nether")
    assert (leave["x"], leave["dim"]) == (-140.0 + 19 * 10.0, "overworld")


async def test_only_the_range_and_the_players_asked_for_are_read(maker) -> None:
    seed = Seed()
    seed.session("castle", FOX, T0, T0 + 600)
    seed.session("castle", MIRA, T0 + 100, T0 + 500)
    seed.walk("castle", FOX, T0, 60)
    seed.walk("castle", MIRA, T0 + 100, 40, z=50.0)
    seed.walk("slot3", FOX, T0, 60, z=-999.0)  # another world entirely
    seed.event("castle", MIRA, "death", T0 + 450, dim="overworld", x=1.0, y=2.0, z=3.0,
               cause="entityAttack", killer="minecraft:creeper")  # fmt: skip
    await seed.into(maker)

    window = await timeline.timeline(maker, "castle", T0 + 200, T0 + 300)
    for trail in window["trails"]:
        times = [p[0] for g in trail["segments"] for p in g["points"]]
        assert min(times) >= T0 + 200 and max(times) <= T0 + 300
        assert all(p[3] != -999.0 for g in trail["segments"] for p in g["points"])
    assert window["events"] == []

    mira = await timeline.timeline(maker, "castle", T0, T0 + 600, players=[MIRA])
    assert [t["gamertag"] for t in mira["trails"]] == ["Mira_P"]
    assert {e["xuid"] for e in mira["events"]} == {MIRA}
    death = next(e for e in mira["events"] if e["kind"] == "death")
    assert (death["cause"], death["killer"]) == ("entityAttack", "minecraft:creeper")
    # The session list is the world's whole history, whoever is filtered.
    assert len(mira["sessions"]) == 2


async def test_the_session_list_is_the_worlds_newest_first_with_its_facts(maker) -> None:
    seed = Seed()
    seed.session("castle", FOX, T0, T0 + 1_000)
    seed.walk("castle", FOX, T0, 11, dx=30.0)  # 300 blocks east
    seed.event("castle", FOX, "death", T0 + 150, dim="overworld", x=300.0, y=64.0, z=0.0,
               cause="fall")  # fmt: skip
    seed.event("castle", FOX, "respawn", T0 + 160, dim="overworld", x=-500.0, y=64.0, z=0.0)
    seed.walk("castle", FOX, T0 + 170, 5, x0=-500.0, dx=10.0)  # 40 more, after the jump
    seed.walk("castle", FOX, T0 + 300, 3, dim="nether", x0=0.0, dx=8.0)  # 16 in the Nether
    seed.session("castle", MIRA, T0 + 2_000, None)  # on now
    seed.walk("castle", MIRA, T0 + 2_000, 2, x0=7.0, z=9.0)
    seed.session("slot3", FOX, T0 + 5_000, T0 + 6_000)  # played in another world
    await seed.into(maker)

    got = await timeline.timeline(maker, "castle", T0, T0 + 3_000, include=frozenset({"sessions"}))

    assert "trails" not in got and "events" not in got
    mira, fox = got["sessions"]
    assert (mira["gamertag"], mira["end"], mira["deaths"]) == ("Mira_P", None, 0)
    assert mira["first"] == {"dim": "overworld", "x": 7.0, "z": 9.0}
    assert (fox["start"], fox["end"], fox["deaths"]) == (T0, T0 + 1_000, 1)
    assert fox["distance"] == {"overworld": 340, "nether": 16}
    assert fox["dims"] == ["overworld", "nether"]
    assert fox["first"] == {"dim": "overworld", "x": 0.0, "z": 0.0}
    assert set(fox) >= {"id", "xuid", "last_seen"}


async def test_the_fog_gives_first_seen_up_to_the_ranges_end(maker) -> None:
    seed = Seed()
    seed.chunk("castle", FOX, 0, 0, T0 + 50, "overworld")
    seed.chunk("castle", MIRA, 0, 0, T0 + 10, "overworld")  # Mira saw it first
    seed.chunk("castle", MIRA, 1, 0, T0 + 20, "overworld")
    seed.chunk("castle", FOX, 9, 9, T0 + 5_000, "overworld")  # after the range
    seed.chunk("castle", FOX, 2, 2, T0 + 30, "nether")
    await seed.into(maker)

    anyone = await timeline.timeline(maker, "castle", T0, T0 + 100, include=frozenset({"explored"}))
    assert anyone["explored"] == {
        "dim": "overworld",
        "chunks": [[0, 0, T0 + 10], [1, 0, T0 + 20]],
        "truncated": False,
    }
    fox = await timeline.timeline(
        maker, "castle", T0, T0 + 100, players=[FOX], include=frozenset({"explored"})
    )
    assert fox["explored"]["chunks"] == [[0, 0, T0 + 50]]
    nether = await timeline.timeline(
        maker, "castle", T0, T0 + 100, include=frozenset({"explored"}), dim="nether"
    )
    assert nether["explored"]["chunks"] == [[2, 2, T0 + 30]]


async def test_a_bad_range_is_refused_before_anything_is_read(maker) -> None:
    with pytest.raises(ValueError, match="after it starts"):
        await timeline.timeline(maker, "castle", T0, T0)
    with pytest.raises(ValueError, match="at most 92 days"):
        await timeline.timeline(maker, "castle", T0, T0 + timeline.MAX_SPAN_S + 1)
    with pytest.raises(ValueError, match="dimension"):
        await timeline.timeline(maker, "castle", T0, T0 + 10, dim="aether")


async def test_the_debug_counts_carry_no_positions(maker) -> None:
    seed = Seed()
    seed.session("castle", FOX, T0, T0 + 30_000)
    seed.walk("castle", FOX, T0, 2_500)
    await seed.into(maker)

    got = await timeline.counts(maker, "castle", T0, T0 + 30_000)

    (player,) = got["players"]
    assert player["samples"] == 2_500 and player["points_sent"] <= timeline.MAX_POINTS
    assert player["downsampled"] is True and player["segments"] == 1
    assert got["events"] == {"join": 1, "leave": 1} and got["sessions"] == 1
    assert set(player) == {
        "xuid", "gamertag", "samples", "points_sent", "segments", "downsampled"
    }  # fmt: skip
