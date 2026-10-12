"""Migration 0239 against real Postgres: the beacons and gates the map and Dave show (§M26).

The contract, case by case:
- the session drain turns the pack's `beacon` / `gate` events into rows, an event patching
  and a snapshot replacing a world's set (a cut snapshot only patching), and replaying the
  same events after an api restart ends in the same rows;
- a world reset or import clears its rows and no other world's;
- `mc_gates_beacons` answers from them: everyone's in the app, the asker's own in game;
- both tables are the owner's alone: any other session reads nothing and writes nothing.
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

from jbrain.agent import minecraftmarkers as mk
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import markers, sessions
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_world_beacons", "mc_world_gates", "mc_places", "mc_player_events")
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
T0 = 1_791_640_000.0
FARM = {"place": "Tree farm", "dim": "overworld", "x": -160, "y": 68, "z": -140}
NETHER_BASE = {"place": "Nether base", "dim": "nether", "x": -20, "y": 72, "z": 40}


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
    """`GET /events?after=` over a scripted list, and `/worlds` for the tool."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.down = False

    def add(self, kind: str, at: float, world: str = "castle", **extra: Any) -> None:
        self.events.append(
            {
                "id": len(self.events) + 1,
                "at": at,
                "kind": kind,
                "name": "",
                "xuid": "",
                "world": world,
                **extra,
            }
        )

    async def call(self, _settings: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if self.down:
            raise HTTPException(status_code=503, detail="stopped")
        if path == "/worlds":
            return {"slots": [{"id": "slot2", "folder": "castle", "exists": True, "active": True}]}
        after = int(kw["params"]["after"])
        return {"boot_id": "b", "events": [e for e in self.events if e["id"] > after]}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> FakeSidecar:
    fake = FakeSidecar()
    monkeypatch.setattr(mc, "call", fake.call)
    return fake


def beacon(op: str, x: int, z: int, by: str = "Mira_P", dim: str = "overworld") -> dict:
    return {"op": op, "dim": dim, "x": x, "y": 72, "z": z, "by": by}


def gate(gid: int, op: str, target: dict | None = None, planter: str = "BlockyFox") -> dict:
    return {
        "op": op,
        "id": gid,
        "dim": "overworld",
        "x": 28,
        "y": 71,
        "z": 14 + gid * 10,
        "axis": "x",
        "planter": planter,
        "by": planter,
        "target": target,
    }


async def _drain(maker: async_sessionmaker) -> None:
    await sessions.SessionDrain(maker, SimpleNamespace()).tick()


async def _rows(maker: async_sessionmaker, world: str = "castle") -> dict[str, Any]:
    return await markers.for_world(maker, world)


async def test_events_patch_and_a_snapshot_replaces_a_worlds_rows(maker, sidecar) -> None:
    sidecar.add("beacon", T0, **beacon("on", 60, -40))
    sidecar.add("beacon", T0 + 1, **beacon("on", 90, -10, by="BlockyFox"))
    sidecar.add("beacon", T0 + 2, **beacon("off", 90, -10, by="Steve"))
    sidecar.add("gate", T0 + 3, **gate(1, "planted"))
    sidecar.add("gate", T0 + 4, **gate(1, "opened", FARM))
    sidecar.add("gate", T0 + 5, **gate(2, "planted", planter="Mira_P"))
    sidecar.add("gate", T0 + 6, **gate(3, "planted"))
    sidecar.add("gate", T0 + 7, **gate(3, "collapsed"))
    sidecar.add("beacon", T0 + 8, world="other", **beacon("on", 0, 0))
    await _drain(maker)
    got = await _rows(maker)
    assert [(b["x"], b["placed_by"], b["placed_at"]) for b in got["beacons"]] == [
        (60, "Mira_P", T0)
    ]
    one, two = got["gates"]
    assert (one["id"], one["state"], one["target"], one["planted_at"], one["aimed_at"]) == (
        1,
        "open",
        FARM,
        T0 + 3,
        T0 + 4,
    )
    assert (two["id"], two["planter"], two["state"], two["target"]) == (
        2,
        "Mira_P",
        "dormant",
        None,
    )
    assert got["as_of"] == T0 + 5  # the newest row it holds

    # The next load's snapshot: gate 2 is gone, gate 1 was re-aimed while no one heard,
    # a beacon arrived from an older pack (no placer), and the known one is kept as it was.
    sidecar.add(
        "beacons",
        T0 + 100,
        all=[beacon("", 60, -40, by="")] + [beacon("", -150, 96, by="")],
        truncated=False,
    )
    sidecar.add(
        "gates",
        T0 + 101,
        all=[{**gate(1, ""), "target": NETHER_BASE}],
        truncated=False,
    )
    await _drain(maker)
    got = await _rows(maker)
    assert [(b["x"], b["placed_by"], b["placed_at"]) for b in got["beacons"]] == [
        (60, "Mira_P", T0),
        (-150, "", None),
    ]
    (only,) = got["gates"]
    # A target that moved unheard: the new one, its time unknown; the planting kept.
    assert (only["target"], only["aimed_at"], only["planted_at"]) == (NETHER_BASE, None, T0 + 3)

    # A cut snapshot only patches: what it doesn't list stays.
    sidecar.add("beacons", T0 + 200, all=[beacon("", 5, 5, by="")], truncated=True)
    await _drain(maker)
    assert {b["x"] for b in (await _rows(maker))["beacons"]} == {60, -150, 5}
    assert [b["x"] for b in (await _rows(maker, "other"))["beacons"]] == [0]
    # One dimension at a time for the route.
    assert (await markers.for_world(maker, "castle", "nether"))["beacons"] == []


async def test_replaying_the_same_events_ends_in_the_same_rows(maker, sidecar) -> None:
    sidecar.add("gate", T0, **gate(1, "planted"))
    sidecar.add("gate", T0 + 1, **gate(1, "opened", FARM))
    sidecar.add("gate", T0 + 2, **gate(1, "dormant"))
    sidecar.add("beacon", T0 + 3, **beacon("on", 60, -40))
    await _drain(maker)
    first = await _rows(maker)
    await _drain(maker)  # a fresh drain (an api restart) reads them all again
    assert await _rows(maker) == first
    assert first["gates"][0]["state"] == "dormant" and first["gates"][0]["target"] is None


async def test_a_replaced_world_loses_its_beacons_and_gates(maker, sidecar) -> None:
    sidecar.add("beacon", T0, **beacon("on", 60, -40))
    sidecar.add("gate", T0, **gate(1, "planted"))
    sidecar.add("beacon", T0, world="other", **beacon("on", 1, 1))
    sidecar.add("world_replaced", T0 + 10)
    sidecar.add("beacon", T0 + 20, **beacon("on", 7, 7))  # the new world's first
    await _drain(maker)
    got = await _rows(maker)
    assert [b["x"] for b in got["beacons"]] == [7] and got["gates"] == []
    assert len((await _rows(maker, "other"))["beacons"]) == 1
    # The debug route's counts: never a position.
    assert await markers.summary(maker) == [
        {"world": "castle", "beacons": 1, "gates_open": 0, "gates_dormant": 0, "as_of": T0 + 20},
        {"world": "other", "beacons": 1, "gates_open": 0, "gates_dormant": 0, "as_of": T0},
    ]


async def _place(maker: async_sessionmaker, key: str, name: str, x: int, z: int) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                " VALUES (:k, 'BlockyFox', 'castle', :n, :x, 64, :z, 'overworld')"
            ),
            {"k": key, "n": name, "x": x, "z": z},
        )


async def test_dave_answers_from_the_rows_the_asker_only_their_own(maker, sidecar) -> None:
    sidecar.add("beacon", T0, **beacon("on", 60, -40))
    sidecar.add("beacon", T0, **beacon("on", 90, -10, by="BlockyFox"))
    sidecar.add("gate", T0, **gate(1, "opened", FARM))
    sidecar.add("gate", T0, **gate(2, "planted", planter="Mira_P"))
    await _drain(maker)
    await _place(maker, "111", "home", 70, -30)
    tool = mk.build_minecraft_markers_handlers(maker, SimpleNamespace(), None)["mc_gates_beacons"]

    app = ToolContext(session=OWNER, scopes=())
    out = await tool({}, app)
    assert isinstance(out, ToolOutput) and out.result_brief == "2 gates · 2 beacons"
    assert "Gate 2 (Mira_P's)" in out and "leads to Tree farm" in out

    asker = Asker(
        "111",
        "BlockyFox",
        "castle",
        Spot(30, 70, 30, "overworld"),
        known=frozenset({"tricorder", "no_spawn_beacon", "wormhole_seed"}),
    )
    ctx = ToolContext(session=OWNER, scopes=(), agent_session_id="door-1")
    with asking("door-1", asker):
        mine = await tool({"player": "Mira_P"}, ctx)  # in game a name is never taken
    assert isinstance(mine, ToolOutput) and mine.result_brief == "1 gate · 1 beacon"
    assert "Gate 2" not in mine and "Mira_P" not in mine
    assert "- home: protected by a No-Spawn Beacon." in mine  # Mira_P's: not said where


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED"])
@pytest.mark.parametrize("table", ["mc_world_beacons", "mc_world_gates"])
async def test_a_non_owner_sees_nothing_and_cannot_write(maker, sidecar, ctx_name, table) -> None:
    ctx = {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    sidecar.add("beacon", T0, **beacon("on", 60, -40))
    sidecar.add("gate", T0, **gate(1, "planted"))
    await _drain(maker)
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar() == 1
    async with scoped_session(maker, ctx) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar() == 0
    assert (await markers.for_world(maker, "castle", None, ctx)) == {
        "beacons": [],
        "gates": [],
        "as_of": None,
    }
    inserts = {
        "mc_world_beacons": "INSERT INTO app.mc_world_beacons (world, dim, x, y, z)"
        " VALUES ('w', 'overworld', 0, 0, 0)",
        "mc_world_gates": "INSERT INTO app.mc_world_gates (world, gate_id, dim, x, y, z, axis,"
        " state) VALUES ('w', 9, 'overworld', 0, 0, 0, 'x', 'dormant')",
    }
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(text(inserts[table]))
    async with scoped_session(maker, ctx) as s:
        await s.execute(text(f"DELETE FROM app.{table}"))  # sees nothing, so removes nothing
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar() == 1
