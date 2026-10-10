"""Migration 0225 against real Postgres: the travel log is owner-only, replays safely,
and forgets a world's history when its terrain is replaced.

The contract, case by case:
- a sample stores once however often it's replayed, and clears the fog around it;
- the fog's first/last-seen window only ever widens;
- deaths and joins become timeline events with their place and cause;
- a reset or import deletes that world's trail, fog and events — and a sample of the
  old terrain that drains after the reset doesn't come back;
- nobody but the owner can read or write any of the three tables.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import sessions, travel
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
T0 = 1_791_640_000.0
TABLES = ("mc_player_track", "mc_player_explored", "mc_player_events", "mc_player_sessions")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


class FakeSidecar:
    """`/track` and `/events` over scripted lists, under one boot."""

    def __init__(self) -> None:
        self.boot = "boot-a"
        self.samples: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.down = False

    def sample(self, at: float, x: float, z: float, *, world: str = "world") -> None:
        self.samples.append(
            {
                "id": len(self.samples) + 1,
                "at": at,
                "name": "Steve42",
                "xuid": "111",
                "world": world,
                "dim": "overworld",
                "x": x,
                "y": 64.0,
                "z": z,
                "yaw": 90.0,
            }
        )

    def event(self, kind: str, at: float, *, world: str = "world", **extra: Any) -> None:
        self.events.append(
            {
                "id": len(self.events) + 1,
                "at": at,
                "kind": kind,
                "name": "Steve42" if kind != "world_replaced" else "",
                "xuid": "111" if kind != "world_replaced" else "",
                "world": world,
                **extra,
            }
        )

    async def call(self, _settings: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if self.down:
            raise HTTPException(status_code=503, detail="stopped")
        after = int(kw["params"]["after"])
        if path == "/track":
            return {"boot_id": self.boot, "samples": [t for t in self.samples if t["id"] > after]}
        return {"boot_id": self.boot, "events": [e for e in self.events if e["id"] > after]}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> FakeSidecar:
    fake = FakeSidecar()
    monkeypatch.setattr(mc, "call", fake.call)
    return fake


async def _drain(maker: async_sessionmaker) -> None:
    await sessions.SessionDrain(maker, SimpleNamespace()).tick()
    await travel.TrackDrain(maker, SimpleNamespace()).tick()


async def _count(maker: async_sessionmaker, table: str, where: str = "true") -> int:
    async with scoped_session(maker, OWNER) as s:
        got = (await s.execute(text(f"SELECT count(*) FROM app.{table} WHERE {where}"))).scalar()
    return int(got or 0)


async def test_a_sample_stores_once_and_clears_the_fog_around_it(maker, sidecar) -> None:
    sidecar.sample(T0, 8.0, 8.0)  # chunk (0, 0)
    await _drain(maker)
    await _drain(maker)  # fresh drains re-read from 0
    assert await _count(maker, "mc_player_track") == 1
    side = 2 * travel.REVEAL_CHUNKS + 1
    assert await _count(maker, "mc_player_explored") == side * side
    assert await _count(maker, "mc_player_explored", "cx = -4 AND cz = 4") == 1


async def test_the_fog_window_only_widens(maker, sidecar) -> None:
    sidecar.sample(T0 + 100, 8.0, 8.0)
    sidecar.sample(T0, 9.0, 9.0)  # an older sample drained later
    await _drain(maker)
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    "SELECT extract(epoch FROM first_seen) AS f, extract(epoch FROM last_seen) AS l"
                    " FROM app.mc_player_explored WHERE cx = 0 AND cz = 0"
                )
            )
        ).one()
    assert (row.f, row.l) == (T0, T0 + 100)


async def test_deaths_and_joins_land_on_the_timeline(maker, sidecar) -> None:
    sidecar.event("join", T0)
    sidecar.event(
        "death", T0 + 60, dim="overworld", x=1.5, y=64.0, z=-3.0, cause="fall", killer="x" * 0
    )
    sidecar.event("server_start", T0 + 70)  # not a timeline kind
    await _drain(maker)
    await _drain(maker)
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text("SELECT kind, xuid, x, detail FROM app.mc_player_events ORDER BY at")
            )
        ).all()
    assert [(r.kind, r.xuid) for r in rows] == [("join", "111"), ("death", "111")]
    assert rows[1].x == 1.5 and rows[1].detail == {"cause": "fall", "killer": ""}


async def test_a_replaced_world_forgets_its_history_for_good(maker, sidecar) -> None:
    sidecar.sample(T0, 8.0, 8.0, world="slot2")
    sidecar.sample(T0, 8.0, 8.0, world="world")  # another world keeps its history
    sidecar.event("death", T0 + 5, world="slot2", cause="lava")
    await _drain(maker)
    sidecar.event("world_replaced", T0 + 100, world="slot2")
    sidecar.sample(T0 + 50, 30.0, 30.0, world="slot2")  # old terrain, drained late
    sidecar.sample(T0 + 200, 40.0, 40.0, world="slot2")  # the new terrain's first step
    await _drain(maker)
    assert await _count(maker, "mc_player_track", "world = 'slot2'") == 1
    assert await _count(maker, "mc_player_track", "world = 'world'") == 1
    assert await _count(maker, "mc_player_events", "world = 'slot2' AND kind = 'death'") == 0
    assert await _count(maker, "mc_player_events", "kind = 'world_replaced'") == 1
    # All fog left in slot2 was uncovered on the new terrain, none on the old.
    old = f"world = 'slot2' AND first_seen < to_timestamp({T0 + 100})"
    assert await _count(maker, "mc_player_explored", old) == 0
    assert await _count(maker, "mc_player_explored", "world = 'slot2'") > 0


async def test_a_stopped_sidecar_is_a_quiet_no_op(maker, sidecar) -> None:
    sidecar.down = True
    assert await travel.TrackDrain(maker, SimpleNamespace()).tick() == 0


async def test_the_summary_counts_what_was_recorded(maker, sidecar) -> None:
    sidecar.sample(T0, 8.0, 8.0)
    sidecar.sample(T0 + 10, 100.0, 8.0)
    sidecar.event("death", T0 + 20, cause="drowning")
    await _drain(maker)
    (row,) = await travel.summary(maker)
    assert (row["world"], row["gamertag"], row["samples"], row["deaths"]) == (
        "world",
        "Steve42",
        2,
        1,
    )
    assert row["chunks_explored"] > 81  # two reveal squares, overlapping


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED"])
@pytest.mark.parametrize("table", ["mc_player_track", "mc_player_explored", "mc_player_events"])
async def test_a_non_owner_sees_nothing_and_cannot_write(maker, sidecar, ctx_name, table) -> None:
    ctx = {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    sidecar.sample(T0, 8.0, 8.0)
    sidecar.event("join", T0)
    await _drain(maker)
    assert await _count(maker, table) > 0
    async with scoped_session(maker, ctx) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar() == 0
    inserts = {
        "mc_player_track": "INSERT INTO app.mc_player_track (world, xuid, gamertag, boot_id,"
        " sample_id, at, dim, x, y, z) VALUES ('w','x','x','b',1,now(),'o',0,0,0)",
        "mc_player_explored": "INSERT INTO app.mc_player_explored (world, xuid, dim, cx, cz,"
        " first_seen, last_seen) VALUES ('w','x','o',0,0,now(),now())",
        "mc_player_events": "INSERT INTO app.mc_player_events (world, xuid, gamertag, boot_id,"
        " event_id, at, kind) VALUES ('w','x','x','b',1,now(),'join')",
    }
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(text(inserts[table]))
