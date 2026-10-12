"""`mc_transport` against real Postgres (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M10,
transport on request), with the sidecar and the jbrain pack's console answers faked.

The contract:
- only the asker's own saved places in this world, never someone else's of the same name;
- the pack gets the place and the dimensions the travel log has seen them in, and decides;
- a refusal (no pack, no Tricorder, switch off, a dimension never visited) spends nothing
  and logs nothing;
- a No on the form, or no answer, logs nothing; a trip, and a landing that stopped it,
  are written to their progress log as `auto` entries (who, from, to, the pack).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import minecrafttransport as mt
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.conftest import docker_available
from tests.integration.test_rls import UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

OWNER = SessionContext(principal_kind="owner")
TABLES = ("mc_places", "mc_goal_log", "mc_player_track")
Pack = Callable[[dict[str, Any]], tuple[list[str], list[str]]]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in TABLES:
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


def _line(trip: str, outcome: str, **detail: Any) -> str:
    return f"[x INFO] [Scripting] [jbrain] transport {trip} {outcome} {json.dumps(detail)}"


def offered_then(outcome: str, **detail: Any) -> Pack:
    """The pack offering the trip, then the form ending in `outcome`."""

    def pack(event: dict[str, Any]) -> tuple[list[str], list[str]]:
        trip = event["id"]
        offer = _line(trip, "offered", text="Market — 1,239 blocks, Overworld.", packs=3)
        return [offer], [offer, _line(trip, outcome, **detail)]

    return pack


def refused(why: str) -> Pack:
    def pack(event: dict[str, Any]) -> tuple[list[str], list[str]]:
        return [_line(event["id"], "refused", why=why)], []

    return pack


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"events": [], "logs": [], "pack": refused("no-pack")}

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        if path == "/command":
            command = kw["json"]["command"]
            assert command.startswith("scriptevent jb:transport ")
            event = json.loads(command.removeprefix("scriptevent jb:transport "))
            state["events"].append(event)
            now, later = state["pack"](event)
            state["logs"] = [*state["logs"], *later]
            return {"lines": now}
        if path == "/logs":
            assert kw["params"] == {"tail": mt.LOG_TAIL}
            return {"lines": [{"seq": i, "text": t} for i, t in enumerate(state["logs"])]}
        raise AssertionError(path)

    monkeypatch.setattr(mc, "call", call)
    return state


async def _no_wait(_s: float) -> None:
    return None


def _run(maker: async_sessionmaker) -> Any:
    return mt.build_minecraft_transport_handlers(maker, object(), sleep=_no_wait)["mc_transport"]


def _ctx() -> ToolContext:
    return ToolContext(session=OWNER, scopes=(), agent_session_id=str(uuid.uuid4()))


ASKER = Asker(
    key="111",
    gamertag="Steve 42",
    world="slot2",
    at=Spot(0, 64, 0, "overworld"),
    known=frozenset({"tricorder", "power_pack", "power_pack_uses"}),
)


async def _place(maker, xuid: str, gamertag: str, world: str, name: str, spot: Spot) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                " VALUES (:k, :g, :w, :n, :x, :y, :z, :d)"
            ),
            {
                "k": xuid,
                "g": gamertag,
                "w": world,
                "n": name,
                "x": spot.x,
                "y": spot.y,
                "z": spot.z,
                "d": spot.dim,
            },
        )


async def _walked(maker, xuid: str, world: str, dim: str) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_player_track (world, xuid, gamertag, boot_id, sample_id,"
                " at, dim, x, y, z) VALUES (:w, :k, 'Steve 42', :b, 1, now(), :d, 0, 64, 0)"
            ),
            {"w": world, "k": xuid, "b": str(uuid.uuid4()), "d": dim},
        )


async def _log(maker) -> list[tuple[str, str, str]]:
    async with scoped_session(maker, OWNER) as s:
        rows = await s.execute(
            text("SELECT xuid, source, text FROM app.mc_goal_log ORDER BY at, id")
        )
        return [(r.xuid, r.source, r.text) for r in rows]


async def test_only_the_askers_own_places_in_this_world(maker, sidecar) -> None:
    await _place(maker, "222", "Mira", "slot2", "Market", Spot(1200, 70, 310, "overworld"))
    await _place(maker, "111", "Steve 42", "slot1", "Market", Spot(5, 70, 5, "overworld"))
    await _place(maker, "111", "Steve 42", "slot2", "Home", Spot(9, 64, 9, "overworld"))
    ctx = _ctx()
    assert ctx.agent_session_id is not None
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "market"}, ctx)
    assert "no saved place called" in str(out) and "Home" in str(out)
    assert "never a bed" in str(out)
    assert sidecar["events"] == []  # nothing reached the pack


async def test_a_trip_hands_the_pack_the_place_and_logs_the_trip(maker, sidecar) -> None:
    market = Spot(1200, 70, 310, "overworld")
    await _place(maker, "111", "Steve 42", "slot2", "Market", market)
    await _walked(maker, "111", "slot2", "nether")
    await _walked(maker, "111", "slot1", "the_end")  # another world's travel doesn't count
    sidecar["pack"] = offered_then(
        "moved",
        player="Steve 42",
        label="Market",
        left=2,
        **{"from": {"x": 0, "y": 64, "z": 0, "dim": "overworld"}},
    )
    ctx = _ctx()
    assert ctx.agent_session_id is not None
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "MARKET"}, ctx)
    [event] = sidecar["events"]
    assert {k: v for k, v in event.items() if k != "id"} == {
        "player": "Steve 42",
        "label": "Market",
        "x": 1200,
        "y": 70,
        "z": 310,
        "dim": "overworld",
        "visited": ["nether"],
    }
    assert "Done" in str(out) and "2 left" in str(out)
    assert isinstance(out, ToolOutput) and out.result_brief == "to Market"
    [(xuid, source, entry)] = await _log(maker)
    assert (xuid, source) == ("111", "auto")
    assert entry == (
        "Transported from (0, 0, y 64) in the Overworld to Market (1200, 310, y 70) in the"
        " Overworld; 1 Power Pack spent, 2 left."
    )


@pytest.mark.parametrize(
    ("why", "says"),
    [
        ("no-pack", "no Power Pack"),
        ("no-tricorder", "own Tricorder"),
        ("off", "isn't allowed in this world"),
        ("unvisited", "haven't been to yet"),
        ("unexplored", "no safe height"),
    ],
)
async def test_a_refusal_spends_and_logs_nothing(maker, sidecar, why: str, says: str) -> None:
    await _place(maker, "111", "Steve 42", "slot2", "Market", Spot(1, 70, 1, "nether"))
    sidecar["pack"] = refused(why)
    ctx = _ctx()
    assert ctx.agent_session_id is not None
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "Market"}, ctx)
    assert isinstance(out, ToolOutput) and out.result_brief == f"refused: {why}"
    assert says in str(out)
    assert await _log(maker) == []


async def test_no_on_the_form_or_no_answer_logs_nothing(maker, sidecar) -> None:
    await _place(maker, "111", "Steve 42", "slot2", "Market", Spot(1, 70, 1, "overworld"))
    ctx = _ctx()
    assert ctx.agent_session_id is not None
    sidecar["pack"] = offered_then("declined")
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "Market"}, ctx)
    assert "chose No" in str(out) and "Nothing was spent" in str(out)

    def silent(event: dict[str, Any]) -> tuple[list[str], list[str]]:
        return [_line(event["id"], "offered", text="…", packs=1)], []

    sidecar["pack"] = silent
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "Market"}, ctx)
    assert isinstance(out, ToolOutput) and out.result_brief == "no answer"
    assert await _log(maker) == []


async def test_a_blocked_landing_keeps_the_pack_and_is_logged(maker, sidecar) -> None:
    await _place(maker, "111", "Steve 42", "slot2", "Market", Spot(1, 70, 1, "overworld"))
    sidecar["pack"] = offered_then("blocked", player="Steve 42", label="Market")
    ctx = _ctx()
    assert ctx.agent_session_id is not None
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "Market"}, ctx)
    assert "blocked" in str(out) and "Nothing was spent" in str(out)
    [(_x, source, entry)] = await _log(maker)
    assert source == "auto" and "stopped at the landing (blocked); pack kept" in entry


async def test_a_silent_pack_is_said_plainly(maker, monkeypatch: pytest.MonkeyPatch) -> None:
    await _place(maker, "111", "Steve 42", "slot2", "Market", Spot(1, 70, 1, "overworld"))

    async def call(_cfg: Any, _method: str, _path: str, **_kw: Any) -> dict[str, Any]:
        return {"lines": ["[x INFO] Unknown command"]}

    monkeypatch.setattr(mc, "call", call)
    ctx = _ctx()
    assert ctx.agent_session_id is not None
    with asking(ctx.agent_session_id, ASKER):
        out = await _run(maker)({"place": "Market"}, ctx)
    assert "didn't answer" in str(out)
