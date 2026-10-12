"""`mc_wormhole_aim` against real Postgres (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M12).

The contract the database holds: a gate opens only to one of the asking player's OWN saved
places in the loaded world, in a dimension they have been to, and the request the pack
carries out names that player, never one the model typed. Removing a saved place tells the
pack, so a gate that led there goes dormant. The live checks (within 4 blocks, their own
gate, the Power Pack) are the pack's, in `deploy/minecraft/jbrain-sim/wormhole.test.mjs`.
No new table: gates live in the world, saved places in `app.mc_places`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttools as mt
from jbrain.agent import minecraftwormhole as mw
from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS, MINECRAFT_DAVE_TOOLS
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

TABLES = ("mc_places", "mc_chat_player", "mc_player_track", "mc_player_sessions")
OPENED = (
    "[x] [Scripting] [jbrain] wormhole opened: gate 1 leads to Tree Farm"
    " (its first opening is free)"
)


class FakeSettings:
    async def minecraft_gamertag(self, _ctx: object) -> str | None:
        return "Steve 42"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, SessionContext(principal_kind="owner")) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
        await s.execute(text("DELETE FROM app.agent_sessions WHERE agent = 'minecraft_dave'"))
    await engine.dispose()


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"commands": []}

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/worlds":
            return {"active": "slot2", "slots": []}
        if path == "/command":
            command = kw["json"]["command"]
            state["commands"].append(command)
            if command.startswith("scriptevent jb:wormhole"):
                return {"lines": [OPENED]}
            return {"lines": ["[x] [Scripting] [jbrain] waypoint set"]}
        return {}

    monkeypatch.setattr(mc, "call", call)
    return state


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def _chat(maker: async_sessionmaker, owner: SessionContext) -> ToolContext:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (:id, :pid, 'minecraft_dave', '{}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
    return ToolContext(session=owner, scopes=(), agent_session_id=sid)


async def _place(maker, owner, key: str, name: str, spot: Spot, world: str = "slot2") -> None:
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                " VALUES (:k, :k, :w, :n, :x, :y, :z, :d)"
            ),
            {"k": key, "w": world, "n": name, "x": spot.x, "y": spot.y, "z": spot.z, "d": spot.dim},
        )


async def _been(maker, owner, key: str, dim: str) -> None:
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_track (world, xuid, gamertag, boot_id, sample_id,"
                " at, dim, x, y, z) VALUES ('slot2', :k, :k, :b, 1, now(), :d, 0, 64, 0)"
            ),
            {"k": key, "b": str(uuid.uuid4()), "d": dim},
        )


def _tools(maker: async_sessionmaker) -> dict[str, Any]:
    return {
        **mt.build_minecraft_handlers(maker, object(), FakeSettings()),  # type: ignore[arg-type]
        **mw.build_minecraft_wormhole_handlers(maker, object(), FakeSettings()),  # type: ignore[arg-type]
    }


def _sent(command: str) -> dict[str, Any]:
    return json.loads(command.removeprefix("scriptevent jb:wormhole "))


def _brief(out: object) -> str | None:
    return getattr(out, "result_brief", None)


def _asker(key: str, gamertag: str, at: Spot) -> Asker:
    return Asker(key=key, gamertag=gamertag, world="slot2", at=at)


def test_the_tool_is_dave_s_in_the_app_and_in_game() -> None:
    assert "mc_wormhole_aim" in MINECRAFT_DAVE_TOOLS
    assert "mc_wormhole_aim" in MINECRAFT_DAVE_INGAME_TOOLS


async def test_in_game_it_opens_to_the_askers_own_place_only(maker, sidecar) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    assert ctx.agent_session_id is not None
    tools = _tools(maker)
    await _place(maker, owner, "111", "Tree Farm", Spot(100, 70, -50, "overworld"))
    await _place(maker, owner, "222", "Mira's Mine", Spot(5, -40, 5, "overworld"))
    await _place(maker, owner, "111", "Old Base", Spot(1, 64, 1, "overworld"), world="slot1")
    steve = _asker("111", "Steve 42", Spot(0, 64, 2, "overworld"))
    with asking(ctx.agent_session_id, steve):
        out = await tools["mc_wormhole_aim"]({"place": "tree farm"}, ctx)
        theirs = await tools["mc_wormhole_aim"]({"place": "Mira's Mine"}, ctx)
        other_world = await tools["mc_wormhole_aim"]({"place": "old base"}, ctx)
    assert _sent(sidecar["commands"][0]) == {
        "op": "aim",
        "player": "Steve 42",
        "place": "Tree Farm",
        "x": 100,
        "y": 70,
        "z": -50,
        "dim": "overworld",
    }
    assert "Opened" in out and "gate 1" in out and "one-way" in out
    assert _brief(out) == "opened → Tree Farm"
    # Someone else's place, or one in another world, is no place of theirs.
    assert "no saved place called" in theirs and "Tree Farm" in theirs
    assert "no saved place called" in other_world
    assert len(sidecar["commands"]) == 1


async def test_a_place_in_a_dimension_they_have_not_been_to_is_refused(maker, sidecar) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    assert ctx.agent_session_id is not None
    tools = _tools(maker)
    await _place(maker, owner, "111", "Fortress", Spot(-20, 80, 7, "nether"))
    await _place(maker, owner, "111", "Outpost", Spot(900, 60, 0, "the_end"))
    steve = _asker("111", "Steve 42", Spot(0, 64, 2, "overworld"))
    with asking(ctx.agent_session_id, steve):
        out = await tools["mc_wormhole_aim"]({"place": "fortress"}, ctx)
        assert _brief(out) == "not visited" and "Nether" in out
        await _been(maker, owner, "111", "nether")
        await _been(maker, owner, "222", "the_end")  # someone else's trip counts for nothing
        await tools["mc_wormhole_aim"]({"place": "fortress"}, ctx)
        end = await tools["mc_wormhole_aim"]({"place": "outpost"}, ctx)
    assert [_sent(c)["place"] for c in sidecar["commands"]] == ["Fortress"]
    assert _brief(end) == "not visited"
    # Standing in the End right now is having been there.
    with asking(ctx.agent_session_id, _asker("111", "Steve 42", Spot(0, 64, 0, "the_end"))):
        await tools["mc_wormhole_aim"]({"place": "outpost"}, ctx)
    assert _sent(sidecar["commands"][-1])["dim"] == "the_end"


async def test_in_the_app_it_aims_the_chat_players_gate(maker, sidecar) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    await _place(maker, owner, "name:steve 42", "Tree Farm", Spot(100, 70, -50, "overworld"))
    out = await _tools(maker)["mc_wormhole_aim"]({"place": "Tree Farm", "gamertag": "Mira"}, ctx)
    assert "Opened" in out
    assert _sent(sidecar["commands"][-1])["player"] == "Steve 42"  # never a typed gamertag


async def test_removing_a_place_tells_the_pack(maker, sidecar) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    assert ctx.agent_session_id is not None
    tools = _tools(maker)
    with asking(ctx.agent_session_id, _asker("111", "Steve 42", Spot(1, 64, 2, "overworld"))):
        await tools["mc_place_save"]({"name": "Tree Farm"}, ctx)
        assert "Removed" in await tools["mc_place_remove"]({"name": "tree farm"}, ctx)
        await tools["mc_place_remove"]({"name": "tree farm"}, ctx)  # gone: nothing to tell
    forgets = [c for c in sidecar["commands"] if c.startswith("scriptevent jb:wormhole")]
    assert [_sent(c) for c in forgets] == [
        {"op": "forget", "player": "Steve 42", "place": "Tree Farm"}
    ]
