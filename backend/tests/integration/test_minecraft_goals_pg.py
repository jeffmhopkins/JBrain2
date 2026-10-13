"""Players' goals in the app, against real Postgres (plan §M24; GUI gate
docs/mocks/minecraft-goals/, variant B, look-only).

The contract, case by case:
- a goal Dave makes is filed under the loaded world (`app.mc_goals.world`), as places are;
- `/goals` reads every player's goals and journal, a goal with no world reading as the
  loaded world's, and each goal's journal capped and newest first;
- `/goals/progress` counts one player's open counted goals, each in its own world, from
  their saved places there and where they stand, and charges no deep scan;
- the goals and journal are the owner's alone: any other session reads nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext
from jbrain.api import minecraft as api
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import goals, scans
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_goal_log", "mc_goals", "mc_places")
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
XUID = "2535411"
WORLDS = {
    "active": "castle",
    "slots": [
        {"id": "slot1", "folder": "castle", "name": "Castle Hill", "exists": True},
        {"id": "slot3", "folder": "sky", "name": "Sky Islands", "exists": True},
    ],
}
T0 = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


class FakeSettings:
    async def minecraft_gamertag(self, _ctx: object) -> str | None:
        return "BlockyFox"


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"stock": [], "carry": 0}

    async def call(_cfg: Any, _m: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/worlds":
            return WORLDS
        if path == "/status":
            return {"players": [{"name": "BlockyFox", "xuid": XUID}]}
        if path == "/command":
            state["carry"] += 1
            return {
                "lines": [
                    "[x INFO] [jbrain] carry counted for BlockyFox in overworld at x=30 y=64"
                    " z=-40: minecraft:iron_block=9; shulker boxes=1"
                ]
            }
        if path == "/map/stock":
            p = kw["params"]
            state["stock"].append(p)
            n = len(p["centres"].split(";"))
            got = {"slot1": 5, "slot3": 120}[p["slot"]]
            totals = [{n_: got for n_ in p["items"].split(",")}] + [{}] * (n - 1)
            return {"centres": [{"totals": t} for t in totals], "age_s": 60, "as_of": 1.0}
        raise AssertionError(path)

    async def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the app never charges a deep scan")

    monkeypatch.setattr(mc, "call", call)
    monkeypatch.setattr(scans, "charge", never)
    return state


async def _seed(maker: async_sessionmaker) -> dict[str, str]:
    """BlockyFox's goals: one with no world (from before goals kept one), one in Sky
    Islands, one plain, one done; a journal; and places in both worlds."""
    ids: dict[str, str] = {}
    async with scoped_session(maker, OWNER) as s:
        for key, world, title, item, n, status, at in (
            ("old", None, "Get 23 iron blocks", "iron_block", 23, "open", T0),
            ("sky", "sky", "Glass for the sky bridge", "glass", 200, "open", T0 + timedelta(1)),
            ("plain", "castle", "Beacon at the base", None, None, "open", T0 + timedelta(2)),
            ("done", "castle", "20 obsidian", "obsidian", 20, "done", T0 + timedelta(3)),
        ):
            ids[key] = str(
                (
                    await s.execute(
                        text(
                            "INSERT INTO app.mc_goals (xuid, gamertag, world, title, item,"
                            " item_count, status, created_at, finished_at) VALUES (:x,"
                            " 'BlockyFox', :w, :t, :i, :n, :st, :at, :fin) RETURNING id"
                        ),
                        {
                            "x": XUID,
                            "w": world,
                            "t": title,
                            "i": item,
                            "n": n,
                            "st": status,
                            "at": at,
                            "fin": None if status == "open" else at,
                        },
                    )
                ).scalar()
            )
        for i in range(12):
            await s.execute(
                text(
                    "INSERT INTO app.mc_goal_log (xuid, gamertag, goal_id, at, text, source)"
                    " VALUES (:x, 'BlockyFox', :g, :at, :t, 'player')"
                ),
                {"x": XUID, "g": ids["old"], "at": T0 + timedelta(hours=i), "t": f"entry {i}"},
            )
        await s.execute(
            text(
                "INSERT INTO app.mc_goal_log (xuid, gamertag, goal_id, at, text, source)"
                " VALUES (:x, 'BlockyFox', NULL, :at, 'built a house', 'dave')"
            ),
            {"x": XUID, "at": T0},
        )
        for name, world, x, z in (("Home", "castle", 40, -30), ("Sky base", "sky", 0, 0)):
            await s.execute(
                text(
                    "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                    " VALUES (:x, 'BlockyFox', :w, :n, :px, 64, :pz, 'overworld')"
                ),
                {"x": XUID, "w": world, "n": name, "px": x, "pz": z},
            )
    return ids


def _request(maker: async_sessionmaker) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)))


async def test_a_goal_dave_makes_is_filed_under_the_loaded_world(maker, sidecar) -> None:
    tools = mt.build_minecraft_handlers(maker, object(), FakeSettings())  # type: ignore[arg-type]
    ctx = ToolContext(session=OWNER, scopes=(), agent_session_id=None)
    await tools["mc_goal_create"]({"title": "Get 64 oak logs", "item": "oak_log", "count": 64}, ctx)
    async with scoped_session(maker, OWNER) as s:
        world = (await s.execute(text("SELECT world FROM app.mc_goals"))).scalar()
    assert world == "castle"


async def test_the_goals_route_reads_every_goal_and_its_journal(maker, sidecar) -> None:
    ids = await _seed(maker)
    body = await api.player_goals(_request(maker), object())
    assert body["world"] == "castle"
    [p] = body["players"]
    assert p["xuid"] == XUID and p["gamertag"] == "BlockyFox"
    got = [(g["title"], g["world"], g["world_name"], g["loaded"]) for g in p["goals"]]
    assert got == [
        ("Get 23 iron blocks", "castle", "Castle Hill", True),
        ("Glass for the sky bridge", "sky", "Sky Islands", False),
        ("Beacon at the base", "castle", "Castle Hill", True),
        ("20 obsidian", "castle", "Castle Hill", True),
    ]
    done = p["goals"][3]
    assert done["status"] == "done" and done["finished_at"] == (T0 + timedelta(3)).timestamp()
    iron = [e for e in p["journal"] if e["goal_id"] == ids["old"]]
    # Capped per goal, newest first; the entry for no goal is kept too.
    assert [e["text"] for e in iron] == [f"entry {i}" for i in range(11, 1, -1)]
    assert [e["text"] for e in p["journal"] if e["goal_id"] is None] == ["built a house"]
    assert {e["source"] for e in p["journal"]} == {"player", "dave"}


async def test_progress_counts_each_goal_in_its_own_world(maker, sidecar) -> None:
    ids = await _seed(maker)
    got = await api.goal_progress(_request(maker), object(), XUID, flush=False)
    assert got["online"] is True
    old, sky = got["goals"]
    assert old["id"] == ids["old"] and sky["id"] == ids["sky"]
    # Castle Hill: 9 carried, 5 near Home, nothing near where they stand.
    assert (old["have"], old["carried"], old["here"], old["boxes"]) == (14, 9, 0, 1)
    assert old["chests"] == [{"place": "Home", "count": 5, "distance": 14, "far": False}]
    # Sky Islands isn't loaded: its saved files, no pockets.
    assert (sky["have"], sky["carried"], sky["carried_why"]) == (120, None, "not_loaded")
    assert [c["place"] for c in sky["chests"]] == ["Sky base"]
    assert sidecar["carry"] == 1
    assert sorted((p["slot"], p["centres"]) for p in sidecar["stock"]) == [
        ("slot1", "40,-30;30,-40"),
        ("slot3", "0,0"),
    ]


async def test_goals_are_the_owners_alone(maker, sidecar) -> None:
    await _seed(maker)
    assert await goals.read_goals(maker, EVERY_SCOPE) == []
    got = await goals.count_progress(maker, mc_sidecar, XUID, flush=False, ctx=EVERY_SCOPE)
    assert got["goals"] == []


async def mc_sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
    return await mc.call(object(), method, path, **kw)
