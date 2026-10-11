"""Migration 0228 against real Postgres: saved places, and the in-game door's sessions.

The contract (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M9/§P2, the owner's three phrases):
- "mark this location as tree farm" saves where the asker stands; saving the name again
  moves it, names ignore case, and from the PWA it defaults to the last tracked spot;
- "navigate to tree farm" points the tricorder at the saved place;
- "point me home" uses a saved home, else the bed the server reported, else says so;
- places written by name move onto the xuid once it's known;
- each player has one continuing in-game session, separate from PWA chats about them,
  rolled over after 6 hours idle; a question runs as a turn recorded in it;
- nobody but the owner can read or write `mc_places`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttools as mt
from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS
from jbrain.agent.loop import AgentResult, ToolContext
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import door as door_mod
from jbrain.minecraft.asker import Asker, Spot, asking
from jbrain.tasks.runner import ExecutedTurn
from tests.conftest import docker_available
from tests.integration.test_rls import UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)
TABLES = ("mc_places", "mc_chat_player", "mc_player_track", "mc_player_sessions")
WAYPOINT_SET = ["[x] [Scripting] [jbrain-probe] waypoint set"]


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
            return {"lines": WAYPOINT_SET if command.startswith("scriptevent") else []}
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


async def _seen(maker, owner, xuid: str, gamertag: str) -> None:
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_sessions"
                " (xuid, gamertag, boot_id, start_event_id, started_at, last_seen_at)"
                " VALUES (:x, :g, :b, 1, now(), now())"
            ),
            {"x": xuid, "g": gamertag, "b": str(uuid.uuid4())},
        )


def _tools(maker: async_sessionmaker) -> dict[str, Any]:
    return mt.build_minecraft_handlers(maker, object(), FakeSettings())  # type: ignore[arg-type]


def _asker(at: Spot, spawn: Spot | None = None) -> Asker:
    return Asker(key="111", gamertag="Steve 42", world="slot2", at=at, spawn=spawn)


def _waypoint(command: str) -> dict[str, Any]:
    return json.loads(command.removeprefix("scriptevent jb:waypoint "))


async def test_the_owners_three_phrases(maker, sidecar) -> None:
    owner = await _owner(maker)
    await _seen(maker, owner, "111", "Steve 42")
    ctx = await _chat(maker, owner)
    assert ctx.agent_session_id is not None
    tools = _tools(maker)
    standing = _asker(Spot(120, 64, -40, "overworld"))
    # "Dave, mark this location as tree farm"
    with asking(ctx.agent_session_id, standing):
        saved = await tools["mc_place_save"]({"name": "Tree  Farm"}, ctx)
    assert "(120, -40, y 64) in the Overworld" in saved and "standing" in saved
    # "Dave, navigate to tree farm", asked from somewhere else.
    elsewhere = _asker(Spot(120, 70, 60, "overworld"))
    with asking(ctx.agent_session_id, elsewhere):
        out = await tools["mc_set_waypoint"]({"place": "tree farm"}, ctx)
        listed = await tools["mc_places"]({}, ctx)
    assert _waypoint(sidecar["commands"][-1]) == {
        "player": "Steve 42",
        "x": 120,
        "y": 64,
        "z": -40,
        "dim": "overworld",
        "label": "Tree Farm",
    }
    assert "From you: 100 blocks north." in out
    assert "Tree Farm: (120, -40, y 64)" in listed and "100 blocks north" in listed
    # "Dave, point me home": no saved home, so the bed the server reported.
    with asking(
        ctx.agent_session_id, _asker(Spot(0, 64, 0, "overworld"), Spot(5, 70, 5, "overworld"))
    ):
        await tools["mc_set_waypoint"]({"place": "home"}, ctx)
    assert _waypoint(sidecar["commands"][-1])["label"] == "Home (your bed)"
    # No bed either: it says so, offers to mark it, and touches nothing.
    sent = len(sidecar["commands"])
    with asking(ctx.agent_session_id, _asker(Spot(0, 64, 0, "overworld"))):
        out = await tools["mc_set_waypoint"]({"place": "home"}, ctx)
        await tools["mc_place_save"]({"name": "home"}, ctx)
        await tools["mc_set_waypoint"]({"place": "HOME"}, ctx)
    assert "no bed" in out and "mark it" in out and "Tree Farm" in out
    assert len(sidecar["commands"]) == sent + 1
    assert _waypoint(sidecar["commands"][-1])["label"] == "home"  # the saved one wins now


async def test_saving_again_moves_and_remove_forgets(maker, sidecar) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    tools = _tools(maker)
    await tools["mc_place_save"]({"name": "base", "x": 1, "y": 64, "z": 2}, ctx)
    moved = await tools["mc_place_save"](
        {"name": "BASE", "x": 9, "z": 9, "dimension": "nether"}, ctx
    )
    assert "moved from (1, 2, y 64) in the Overworld" in moved
    async with scoped_session(maker, owner) as s:
        rows = (await s.execute(text("SELECT name, x, dim, world FROM app.mc_places"))).all()
    assert [tuple(r) for r in rows] == [("BASE", 9, "nether", "slot2")]
    assert "Removed" in await tools["mc_place_remove"]({"name": "base"}, ctx)
    assert "no saved place" in await tools["mc_place_remove"]({"name": "base"}, ctx)
    assert "no saved places" in await tools["mc_places"]({}, ctx)
    assert "outside the world" in await tools["mc_place_save"](
        {"name": "x", "x": 0, "z": 0, "y": 999}, ctx
    )


async def test_from_the_pwa_a_place_defaults_to_the_last_tracked_spot(maker, sidecar) -> None:
    owner = await _owner(maker)
    await _seen(maker, owner, "111", "Steve 42")
    ctx = await _chat(maker, owner)
    tools = _tools(maker)
    assert "no known position" in await tools["mc_place_save"]({"name": "camp"}, ctx)
    async with scoped_session(maker, owner) as s:
        for world, x in (("world", 5.0), ("slot2", 300.6)):
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_track (world, xuid, gamertag, boot_id,"
                    " sample_id, at, dim, x, y, z) VALUES (:w, '111', 'Steve 42', :b, 1,"
                    " now(), 'overworld', :x, 64.2, -40.5)"
                ),
                {"w": world, "b": str(uuid.uuid4()), "x": x},
            )
    out = await tools["mc_place_save"]({"name": "camp"}, ctx)
    assert "(300, -41, y 64)" in out and "last known position" in out


async def test_places_kept_by_name_move_to_the_xuid(maker, sidecar) -> None:
    owner = await _owner(maker)
    async with scoped_session(maker, owner) as s:
        for key, name, x in (
            ("name:steve 42", "home", 1),
            ("name:steve 42", "mine", 2),
            ("111", "Home", 3),
        ):
            await s.execute(
                text(
                    "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, z, dim)"
                    " VALUES (:k, 'Steve 42', 'slot2', :n, :x, 0, 'overworld')"
                ),
                {"k": key, "n": name, "x": x},
            )
    await _seen(maker, owner, "111", "Steve 42")
    ctx = await _chat(maker, owner)
    await _tools(maker)["mc_places"]({}, ctx)  # resolving the player re-keys
    async with scoped_session(maker, owner) as s:
        rows = (await s.execute(text("SELECT xuid, name, x FROM app.mc_places ORDER BY x"))).all()
    # The account's own "Home" wins over the one kept by name.
    assert [tuple(r) for r in rows] == [("111", "mine", 2), ("111", "Home", 3)]


@pytest.mark.parametrize("ctx_name", ["EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_sees_no_places_and_cannot_write_one(maker, sidecar, ctx_name) -> None:
    owner = await _owner(maker)
    ctx = await _chat(maker, owner)
    await _tools(maker)["mc_place_save"]({"name": "base", "x": 1, "z": 2}, ctx)
    other = {"EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    async with scoped_session(maker, other) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.mc_places"))).scalar() == 0
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with scoped_session(maker, other) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, z, dim)"
                    " VALUES ('x', 'x', 'w', 'n', 0, 0, 'overworld')"
                )
            )


class RecordingExecutor:
    def __init__(self, maker: async_sessionmaker) -> None:
        self.maker = maker
        self.calls: list[dict[str, Any]] = []

    async def run_turn(self, **kw: Any) -> ExecutedTurn:
        self.calls.append(kw)
        # The model "calls" a tool mid-turn: it writes for the asker, whoever the chat says.
        ctx = ToolContext(
            session=kw["read_ctx"], scopes=(), agent_session_id=kw["agent_session_id"]
        )
        await _tools(self.maker)["mc_place_save"]({"name": "tree farm"}, ctx)
        return ExecutedTurn(
            result=AgentResult(
                text="Saved tree farm.", stop_reason="end_turn", steps=1, cost_tokens=5
            )
        )


def _door(maker: async_sessionmaker, owner: SessionContext, executor: Any) -> door_mod.DaveDoor:
    async def pid() -> str | None:
        return owner.principal_id

    return door_mod.DaveDoor(
        maker,
        object(),
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        executor=executor,
        owner_principal_id=pid,
    )


async def test_a_question_runs_in_the_askers_own_continuing_session(maker, sidecar) -> None:
    owner = await _owner(maker)
    await _seen(maker, owner, "111", "Steve 42")
    pwa = await _chat(maker, owner)
    await _tools(maker)["mc_player"]({}, pwa)  # a PWA chat about the same player
    executor = RecordingExecutor(maker)
    door = _door(maker, owner, executor)
    event = {
        "id": 1,
        "kind": "dave",
        "name": "Steve 42",
        "xuid": "111",
        "world": "slot2",
        "text": "Dave, mark this location as tree farm",
        "x": 120.5,
        "y": 64.0,
        "z": -39.5,
        "dim": "overworld",
        "spawn": None,
    }
    q = door_mod.parse_question(event)
    assert q is not None
    await door.answer(q)
    (call,) = executor.calls
    sid = call["agent_session_id"]
    assert sid != pwa.agent_session_id
    assert call["profile"].tools == MINECRAFT_DAVE_INGAME_TOOLS
    assert "Steve 42" in call["profile"].prompt  # the chat intro: whose session it is
    async with scoped_session(maker, owner) as s:
        place = (await s.execute(text("SELECT xuid, x, z FROM app.mc_places"))).one()
        door_rows = (
            (await s.execute(text("SELECT agent_session_id FROM app.mc_chat_player WHERE door")))
            .scalars()
            .all()
        )
        turns = (
            await s.execute(
                text(
                    "SELECT role, content FROM app.agent_turns WHERE session_id = :s ORDER BY seq"
                ),
                {"s": sid},
            )
        ).all()
    assert tuple(place) == ("111", 120, -40)  # where they stood, floored to the block
    assert [str(r) for r in door_rows] == [sid]
    assert [tuple(t) for t in turns] == [
        ("user", "mark this location as tree farm"),
        ("assistant", "Saved tree farm."),
    ]
    assert sidecar["commands"][-1] == (
        'tellraw "Steve 42" {"rawtext": [{"text": "<Dave> Saved tree farm."}]}'
    )
    # The next question continues the same session…
    await door.answer(q)
    assert executor.calls[1]["agent_session_id"] == sid
    # …until it has been idle past the rollover.
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text("UPDATE app.agent_sessions SET last_active_at = :t WHERE id = :s"),
            {"t": datetime.now(UTC) - door_mod.IDLE_ROLLOVER - timedelta(minutes=1), "s": sid},
        )
    await door.answer(q)
    assert executor.calls[2]["agent_session_id"] != sid
