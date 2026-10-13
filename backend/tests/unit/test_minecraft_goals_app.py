"""The app's look at players' goals (plan §M24; GUI gate docs/mocks/minecraft-goals/, B).

The contract: the app counts a goal exactly as Dave does in game — what the player
carries, plus the containers within 48 blocks of each saved place and of where they
stand, each container once — batched per player (one carry and one stock count a
dimension for all their goals in a world), and never spends a deep scan. A far place is
counted and only marked far. A goal is counted in its own world, a goal with no world in
the loaded one; a world that isn't loaded is counted from its files, without pockets.
The database side runs against real Postgres in test_minecraft_goals_pg.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import minecraft as api
from jbrain.api.deps import owner_only
from jbrain.minecraft import client as mc
from jbrain.minecraft import goals, scans

XUID = "2535411"
SETTINGS: Any = SimpleNamespace(minecraft_url="http://mc", minecraft_token="tok")
WORLDS = {
    "active": "castle",
    "slots": [
        {"id": "slot1", "folder": "castle", "name": "Castle Hill", "exists": True},
        {"id": "slot3", "folder": "sky", "name": "Sky Islands", "exists": True},
        {"id": "slot4", "folder": "", "name": None, "exists": False},
    ],
}


def _goal(n: int, item: str, count: int, world: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"g{n}", gamertag="BlockyFox", world=world, item=item, item_count=count
    )


def _place(name: str, x: int, z: int, world: str = "castle", dim: str = "overworld"):
    return SimpleNamespace(name=name, world=world, x=x, y=64, z=z, dim=dim)


class Sidecar:
    """The sidecar as the count sees it: `/worlds`, `/status`, `jb:carry` and
    `/map/stock`, whose chests are keyed "slot dim x,z" → {item: n}."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.online = True
        self.at = (30, -40, "overworld")
        self.pocket: dict[str, int] = {"minecraft:iron_block": 9}
        self.boxes = 1
        self.chests: dict[str, dict[str, int]] = {}
        self.down: set[str] = set()
        self.age_s = 180.0

    async def __call__(self, method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        if path in self.down:
            return "the server is stopped"
        if path == "/worlds":
            self.calls.append(("worlds", None))
            return WORLDS
        if path == "/status":
            players = [{"name": "BlockyFox", "xuid": XUID}] if self.online else []
            return {"players": players}
        if path == "/command":
            query = json.loads(kw["json"]["command"].removeprefix("scriptevent jb:carry "))
            self.calls.append(("carry", query))
            x, z, dim = self.at
            counts = " ".join(f"{t}={self.pocket.get(t, 0)}" for t in query["types"])
            return {
                "lines": [
                    f"[x INFO] [jbrain] carry counted for BlockyFox in {dim} at x={x} y=64"
                    f" z={z}: {counts}; shulker boxes={self.boxes}"
                ]
            }
        if path == "/map/stock":
            params = kw["params"]
            self.calls.append(("stock", params))
            names = params["items"].split(",")
            centres = []
            for c in params["centres"].split(";"):
                held = self.chests.get(f"{params['slot']} {params['dim']} {c}", {})
                centres.append({"totals": {n: held[n] for n in names if n in held}})
            return {"centres": centres, "age_s": self.age_s, "as_of": 1000.0, "flush": "x"}
        raise AssertionError(path)

    def of(self, kind: str) -> list[Any]:
        return [p for k, p in self.calls if k == kind]


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    state: dict[str, list[Any]] = {
        "goals": [_goal(1, "iron_block", 23)],
        "places": [_place("Home", 40, -30), _place("Mine outpost", 470, -460)],
    }

    @asynccontextmanager
    async def session(_maker: Any, _ctx: Any) -> AsyncIterator[object]:
        yield object()

    async def player_rows(_s: Any, xuid: str) -> tuple[list[Any], list[Any]]:
        assert xuid == XUID
        return list(state["goals"]), list(state["places"])

    async def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the app never charges a deep scan")

    monkeypatch.setattr(goals, "scoped_session", session)
    monkeypatch.setattr(goals, "_player_rows", player_rows)
    monkeypatch.setattr(scans, "charge", never)
    return state


async def count(side: Sidecar, flush: bool = False) -> dict[str, Any]:
    return await goals.count_progress(object(), side, XUID, flush=flush)  # type: ignore[arg-type]


async def test_a_goal_counts_pockets_places_and_where_they_stand(rows) -> None:
    side = Sidecar()
    side.chests = {
        "slot1 overworld 40,-30": {"minecraft:iron_block": 5},
        "slot1 overworld 30,-40": {"minecraft:iron_block": 2},
    }
    got = await count(side)
    assert got["online"] is True
    [g] = got["goals"]
    assert (g["have"], g["need"], g["carried"], g["here"], g["boxes"]) == (16, 23, 9, 2, 1)
    assert g["chests"] == [
        {"place": "Home", "count": 5, "distance": 14, "far": False},
        {"place": "Mine outpost", "count": 0, "distance": 608, "far": True},
    ]
    assert (g["world"], g["world_name"], g["loaded"]) == ("castle", "Castle Hill", True)
    assert (g["age_s"], g["as_of"], g["carried_why"], g["chests_why"]) == (
        180.0,
        1000.0,
        None,
        None,
    )
    # Places first, where they stand last, as Dave names the centres.
    [stock] = side.of("stock")
    assert stock["centres"] == "40,-30;470,-460;30,-40"
    assert stock["slot"] == "slot1" and stock["radius"] == 48 and stock["flush"] == 0


async def test_one_carry_and_one_stock_count_cover_all_of_a_players_goals(rows) -> None:
    rows["goals"] = [_goal(1, "iron_block", 23), _goal(2, "oak_log", 64), _goal(3, "torch", 16)]
    rows["places"].append(_place("Nether base", 5, 5, dim="nether"))
    side = Sidecar()
    got = await count(side, flush=True)
    assert [g["id"] for g in got["goals"]] == ["g1", "g2", "g3"]
    [carry] = side.of("carry")
    assert carry == {
        "player": "BlockyFox",
        "types": ["minecraft:iron_block", "minecraft:oak_log", "minecraft:torch"],
    }
    stocks = side.of("stock")
    assert [s["dim"] for s in stocks] == ["overworld", "nether"]
    assert all(
        s["items"] == "minecraft:iron_block,minecraft:oak_log,minecraft:torch" for s in stocks
    )
    assert all(s["flush"] == 1 for s in stocks)
    nether = next(c for c in got["goals"][0]["chests"] if c["place"] == "Nether base")
    assert nether == {"place": "Nether base", "count": 0, "distance": None, "far": True}


async def test_more_than_eight_items_are_asked_in_eights(rows) -> None:
    rows["goals"] = [_goal(i, f"item_{i:02d}", 1) for i in range(10)]
    side = Sidecar()
    await count(side)
    assert [len(c["types"]) for c in side.of("carry")] == [8, 2]
    assert [len(s["items"].split(",")) for s in side.of("stock")] == [8, 2]


async def test_an_offline_player_counts_chests_only_and_nothing_is_far(rows) -> None:
    side = Sidecar()
    side.online = False
    side.chests = {"slot1 overworld 40,-30": {"minecraft:iron_block": 40}}
    [g] = (await count(side))["goals"]
    assert (g["have"], g["carried"], g["carried_why"], g["here"]) == (40, None, "offline", None)
    assert not side.of("carry")
    assert all(c["far"] is False and c["distance"] is None for c in g["chests"])
    assert side.of("stock")[0]["centres"] == "40,-30;470,-460"


async def test_a_goal_in_a_world_that_isnt_loaded_is_counted_from_its_files(rows) -> None:
    rows["goals"] = [_goal(7, "glass", 200, world="sky"), _goal(1, "iron_block", 23)]
    rows["places"].append(_place("Sky base", 0, 0, world="sky"))
    side = Sidecar()
    side.chests = {"slot3 overworld 0,0": {"minecraft:glass": 120}}
    got = await count(side)
    sky, castle = got["goals"]
    assert (sky["id"], sky["loaded"], sky["world_name"]) == ("g7", False, "Sky Islands")
    assert (sky["have"], sky["carried"], sky["carried_why"]) == (120, None, "not_loaded")
    assert [c["place"] for c in sky["chests"]] == ["Sky base"]
    assert castle["loaded"] is True and castle["carried"] == 9
    # Pockets are asked once, for the loaded world's goals only.
    assert [c["types"] for c in side.of("carry")] == [["minecraft:iron_block"]]
    assert {s["slot"] for s in side.of("stock")} == {"slot1", "slot3"}


async def test_nothing_to_count_says_why(rows) -> None:
    side = Sidecar()
    side.online = False
    rows["places"] = []
    [g] = (await count(side))["goals"]
    assert (g["have"], g["carried_why"], g["chests_why"]) == (None, "offline", "no_places")
    assert not side.of("stock")
    rows["goals"] = [_goal(9, "glass", 5, world="gone")]
    [g] = (await count(Sidecar()))["goals"]
    assert (g["chests_why"], g["world_name"], g["loaded"]) == ("no_world", None, False)


async def test_a_stock_count_that_fails_keeps_the_pockets(rows) -> None:
    side = Sidecar()
    side.down = {"/map/stock"}
    [g] = (await count(side))["goals"]
    assert (g["have"], g["chests_why"], g["detail"]) == (9, "failed", "the server is stopped")


async def test_no_worlds_list_is_unavailable(rows) -> None:
    side = Sidecar()
    side.down = {"/worlds"}
    with pytest.raises(goals.Unavailable, match="stopped"):
        await count(side)


def test_world_names_reads_the_slots_that_hold_a_world() -> None:
    active, names = goals.world_names(WORLDS)
    assert active == "castle" and set(names) == {"castle", "sky"}
    assert goals.world_names("down") == (None, {})


# --- the routes --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Sidecar]:
    side = Sidecar()

    async def call(_settings: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        got = await side(method, path, **kw)
        if isinstance(got, str):
            raise HTTPException(status_code=503, detail=got)
        return got

    monkeypatch.setattr(mc, "call", call)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[owner_only] = lambda: SimpleNamespace(id="owner")
    app.state.settings = SETTINGS
    app.state.session_maker = object()
    return TestClient(app), side


def test_the_goals_route_names_each_goals_world(client, monkeypatch) -> None:
    http, side = client
    stored = [
        {"world": None, "id": "a"},
        {"world": "sky", "id": "b"},
        {"world": "gone", "id": "c"},
    ]

    async def read(_maker: Any) -> list[dict[str, Any]]:
        return [{"xuid": XUID, "gamertag": "BlockyFox", "goals": [dict(g) for g in stored]}]

    monkeypatch.setattr(goals, "read_goals", read)
    body = http.get("/api/minecraft/goals").json()
    assert body["world"] == "castle"
    got = [(g["world"], g["world_name"], g["loaded"]) for g in body["players"][0]["goals"]]
    assert got == [
        ("castle", "Castle Hill", True),
        ("sky", "Sky Islands", False),
        ("gone", None, False),
    ]
    side.down = {"/worlds"}
    body = http.get("/api/minecraft/goals").json()
    assert body["world"] is None
    assert [g["loaded"] for g in body["players"][0]["goals"]] == [None, None, None]


def test_the_progress_route_counts_one_player_and_flushes_only_when_asked(client, rows) -> None:
    http, side = client
    got = http.get(f"/api/minecraft/goals/progress?xuid={XUID}")
    assert got.status_code == 200 and got.json()["goals"][0]["carried"] == 9
    assert side.of("stock")[-1]["flush"] == 0
    http.get(f"/api/minecraft/goals/progress?xuid={XUID}&flush=true")
    assert side.of("stock")[-1]["flush"] == 1
    assert http.get("/api/minecraft/goals/progress").status_code == 422
    side.down = {"/worlds"}
    down = http.get(f"/api/minecraft/goals/progress?xuid={XUID}")
    assert down.status_code == 503 and "stopped" in down.json()["detail"]
