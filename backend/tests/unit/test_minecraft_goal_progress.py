"""Goals that count (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M24): "I need 23 iron blocks",
then "how many have I got?", through the real handlers with the database, the sidecar and
the deep-scan ledger faked.

The contract: a goal may carry an item and a count, checked before any write; progress
adds what the asker carries (the pack's `jb:carry`, their own pockets only) to the
containers within 48 blocks of each of their saved places and of where they stand, each
container once; that's free, except in game for a place beyond 48 blocks of the asker,
which is one deep scan per check (a repeat from about here is free, and out of scans the
near part is still given); the app is unlimited; a reached goal is offered as done, never
marked. The writes themselves run against real Postgres in test_minecraft_dave_pg.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft import scans
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.unit.test_minecraft_scans import MemoryBook, MemoryTargets

OWNER = SessionContext(principal_kind="owner")
PWA = ToolContext(session=OWNER, scopes=(), agent_session_id="pwa")
DOOR = ToolContext(session=OWNER, scopes=(), agent_session_id="door")
STEVE = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "overworld"))
DAY = "time query day"


def _goal(n: int, title: str, item: str | None = None, count: int | None = None, **kw: Any):
    return SimpleNamespace(
        id=f"g{n}",
        title=title,
        notes="",
        status=kw.get("status", "open"),
        world=None,
        created_at=None,
        finished_at=None,
        item=item,
        item_count=count,
    )


def _place(name: str, x: int, z: int, dim: str = "overworld"):
    return SimpleNamespace(id=name, name=name, x=x, y=64, z=z, dim=dim)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The player's goals and places, and every SQL statement a handler ran."""
    state: dict[str, Any] = {
        "goals": [_goal(1, "Get 23 iron blocks", "iron_block", 23)],
        "places": [_place("home", 20, 0), _place("tree farm", 0, -30)],
        "sql": [],
    }

    class Session:
        async def execute(self, stmt: Any, params: Any = None) -> Any:
            state["sql"].append(str(stmt))
            return SimpleNamespace(first=lambda: None, scalar=lambda: None)

    @asynccontextmanager
    async def session(_maker: Any, _ctx: Any) -> AsyncIterator[Session]:
        yield Session()

    async def goals(_s: Any, _key: str) -> list[Any]:
        return list(state["goals"])

    async def places(_s: Any, _key: str, _world: str) -> list[Any]:
        return list(state["places"])

    async def chat_player_for(*_a: Any) -> mt.Player:
        return mt.Player("111", "Steve 42")

    monkeypatch.setattr(mt, "scoped_session", session)
    monkeypatch.setattr(mt, "_goals", goals)
    monkeypatch.setattr(mt, "_places", places)
    monkeypatch.setattr(mt, "chat_player_for", chat_player_for)
    return state


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`chests` is what the containers near each point hold, keyed "dim x,z"; `pocket`
    the pack's carry line (or a refusal)."""
    state: dict[str, Any] = {
        "sent": [],
        "stock": [],
        "carry": [],
        "chests": {"overworld 20,0": 12},
        "pocket": "[x INFO] [jbrain] carry counted for Steve 42 in overworld at x=0 y=64"
        " z=0: minecraft:iron_block=5; shulker boxes=0",
        "age_s": 30,
        "down": set(),
    }

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        what = kw["json"]["command"] if path == "/command" else path
        if path in state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        state["sent"].append(what)
        if path == "/worlds":
            return {"active": "slot2", "slots": [{"id": "slot2", "active": True}]}
        if path == "/map/stock":
            params = kw["params"]
            state["stock"].append(params)
            centres = [c for c in params["centres"].split(";") if c]
            per = [
                {params["items"]: state["chests"].get(f"{params['dim']} {c}", 0)} for c in centres
            ]
            return {"centres": [{"totals": t} for t in per], "age_s": state["age_s"]}
        if what == DAY:
            return {"lines": ["[x INFO] Day is 7"]}
        if what.startswith("scriptevent jb:carry "):
            state["carry"].append(json.loads(what.removeprefix("scriptevent jb:carry ")))
            return {"lines": [state["pocket"]]}
        return {"lines": ["[x INFO] ok"]}

    monkeypatch.setattr(mc, "call", call)
    return state


@pytest.fixture
def book() -> MemoryBook:
    return MemoryBook()


def _tools(book: MemoryBook) -> dict[str, Any]:
    return mt.build_minecraft_handlers(
        object(),  # type: ignore[arg-type]
        object(),
        None,
        scan_book=lambda _ctx: book,
        target_book=lambda _ctx: MemoryTargets(),
    )


async def ask(book: MemoryBook, args: dict[str, Any], who: Asker = STEVE) -> Any:
    with asking("door", who):
        return await _tools(book)["mc_goal_progress"](args, DOOR)


# --- the pure pieces --------------------------------------------------------------------


def test_an_item_is_stored_bare_and_checked() -> None:
    assert mt.item_id("Iron Block") == "iron_block"
    assert mt.item_id("minecraft:diamond") == "diamond"
    assert mt.item_id("jbrain:power_pack") == "jbrain:power_pack"
    assert mt.item_id("music_disc_13") == "music_disc_13"
    for bad in ("", None, "x", "iron block!", "op @a", "a" * 70):
        assert mt.item_id(bad) == "", bad
    assert mt.full_id("iron_block") == "minecraft:iron_block"
    assert mt.full_id("jbrain:power_pack") == "jbrain:power_pack"


def test_an_item_and_count_are_checked_before_any_write() -> None:
    assert mt.check_goal_item("iron block", 23) == ("iron_block", 23)
    assert mt.check_goal_item("diamond", "64") == ("diamond", 64)
    assert mt.check_goal_item(None, None) == (None, None)
    assert mt.check_goal_item("", "") == (None, None)
    assert mt.check_goal_item(None, 100_000) == (None, 100_000)
    assert mt.check_goal_item("diamond", 23.0) == ("diamond", 23)
    for count in (0, -1, 100_001, "many", 2.5, True, [3]):
        assert isinstance(mt.check_goal_item("diamond", count), str), count
    assert "item id" in str(mt.check_goal_item("iron blocks!", 3))


def test_the_tally_adds_pockets_and_chests_and_says_where() -> None:
    have, head, where = mt.tally(23, 5, [("home", 12), ("tree farm", 0), ("you", 0)])
    assert (have, head) == (17, "17 of 23 — 6 to go")
    assert where == "12 in chests near home, 5 on you"
    have, head, where = mt.tally(23, None, [("home", 30)])
    assert have == 30 and head == "30 of 23 — that's the goal reached"
    assert where == "30 in chests near home"
    assert mt.tally(10, 0, []) == (0, "0 of 10 — 10 to go", "")


def test_a_goal_line_shows_what_it_counts() -> None:
    assert mt.goal_line(_goal(1, "Get 23 iron blocks", "iron_block", 23)) == (
        "Get 23 iron blocks (iron_block ×23)"
    )
    assert mt.goal_line(_goal(2, "Beacon at the base")) == "Beacon at the base"


# --- creating and listing ---------------------------------------------------------------


async def test_create_takes_an_item_with_its_count(world, book, sidecar) -> None:
    tools = _tools(book)
    out = await tools["mc_goal_create"](
        {"title": "Get 23 iron blocks", "item": "Iron Block", "count": 23}, PWA
    )
    assert "(iron_block ×23; mc_goal_progress" in out
    insert = next(q for q in world["sql"] if "INSERT INTO app.mc_goals" in q)
    assert "item, item_count" in insert
    plain = await tools["mc_goal_create"]({"title": "Beacon"}, PWA)
    assert "×" not in plain


async def test_create_and_update_refuse_a_bad_item_or_count_before_writing(
    world, book, sidecar
) -> None:
    tools = _tools(book)
    for bad in (
        {"title": "Iron", "item": "iron_block"},
        {"title": "Iron", "count": 23},
        {"title": "Iron", "item": "iron_block", "count": 0},
        {"title": "Iron", "item": "iron_block", "count": 100_001},
        {"title": "Iron", "item": "iron; op @a", "count": 3},
    ):
        out = await tools["mc_goal_create"](bad, PWA)
        assert isinstance(out, str) and "Added" not in out, bad
    for bad in (
        {"goal": 1, "count": "lots"},
        {"goal": 1, "item": "!!"},
        {"goal": 1},
    ):
        out = await tools["mc_goal_update"](bad, PWA)
        assert isinstance(out, str), bad
    assert world["sql"] == []
    # A goal that counts nothing yet needs both.
    world["goals"] = [_goal(1, "Beacon")]
    out = await tools["mc_goal_update"]({"goal": 1, "count": 30}, PWA)
    assert "needs both an item and a count" in out
    assert not any("UPDATE" in q for q in world["sql"])


async def test_update_changes_the_count_and_a_status_only_update_still_works(
    world, book, sidecar
) -> None:
    tools = _tools(book)
    out = await tools["mc_goal_update"]({"goal": 1, "count": 30}, PWA)
    assert "now counts iron_block ×30" in out
    world["goals"] = [_goal(1, "Beacon")]
    out = await tools["mc_goal_update"]({"goal": 1, "status": "done"}, PWA)
    assert "now done" in out


async def test_the_goal_list_shows_the_count(world, book, sidecar) -> None:
    world["goals"].append(_goal(2, "Beacon"))
    out = await _tools(book)["mc_goals"]({}, PWA)
    assert "1. [open] Get 23 iron blocks (iron_block ×23)" in out
    assert "2. [open] Beacon" in out


# --- progress, in game ------------------------------------------------------------------


async def test_pockets_and_chests_near_home_add_up_and_cost_nothing(world, book, sidecar) -> None:
    out = await ask(book, {"goal": 1})
    assert "17 of 23 — 6 to go" in out and out.result_brief == "17 of 23"
    assert "12 in chests near home, 5 on you" in out
    assert "Counted the chests within 48 blocks of:" in out
    assert "home, tree farm, you" in out
    assert "deep scan" not in out and book.used(STEVE, 7) == 0
    assert DAY not in sidecar["sent"]  # nothing to charge: the day isn't even asked
    # Their own pockets, by the asker's name; one stock call, places first, then them.
    assert sidecar["carry"] == [{"player": "Steve 42", "types": ["minecraft:iron_block"]}]
    [params] = sidecar["stock"]
    assert params["centres"] == "20,0;0,-30;0,0"
    assert params["radius"] == 48 and params["items"] == "minecraft:iron_block"
    assert params["dim"] == "overworld" and params["flush"] == 1
    assert "Ask whether" not in out


async def test_the_only_counted_goal_is_picked_without_a_number(world, book, sidecar) -> None:
    world["goals"].insert(0, _goal(0, "Beacon"))
    out = await ask(book, {})
    assert "Goal 2," in out and "17 of 23" in out


async def test_a_reached_goal_is_offered_as_done_never_marked(world, book, sidecar) -> None:
    sidecar["chests"]["overworld 20,0"] = 20
    out = await ask(book, {"goal": 1})
    assert "25 of 23 — that's the goal reached" in out
    assert "Ask whether to mark goal 1 done" in out and "only if they say yes" in out
    assert not any("UPDATE" in q or "INSERT" in q for q in world["sql"])
    # A goal already done says the count, and doesn't offer it again.
    world["goals"] = [_goal(1, "Get 23 iron blocks", "iron_block", 23, status="done")]
    again = await ask(book, {"goal": 1})
    assert "that's the goal reached" in again and "Ask whether" not in again


async def test_a_far_place_is_one_deep_scan_and_a_repeat_is_free(world, book, sidecar) -> None:
    world["places"].append(_place("mine", 500, 0))
    sidecar["chests"]["overworld 500,0"] = 4
    out = await ask(book, {"goal": 1})
    assert "21 of 23 — 2 to go" in out and "4 in chests near mine" in out
    assert "That used a deep scan for the far places: 1 scan left today (say so)." in out
    assert book.used(STEVE, 7) == 1
    # One scan for the check, not one per place; and every place in one stock call.
    assert sidecar["stock"][-1]["centres"] == "20,0;0,-30;500,0;0,0"
    # The same check from about here: free, and counted again live.
    sidecar["chests"]["overworld 500,0"] = 6
    nearby = replace(STEVE, at=Spot(10, 64, 10, "overworld"))
    again = await ask(book, {"goal": 1}, nearby)
    assert "23 of 23" in again and "recount is free: 1 scan left today" in again
    assert book.used(STEVE, 7) == 1


async def test_out_of_scans_still_counts_what_is_near(world, book, sidecar) -> None:
    world["places"].append(_place("mine", 500, 0))
    sidecar["chests"]["overworld 500,0"] = 4
    for _ in range(scans.SCANS_PER_DAY):
        await book.reserve(STEVE.world, STEVE.key, STEVE.gamertag, 7)
    out = await ask(book, {"goal": 1})
    assert "17 of 23 — 6 to go" in out
    assert "weren't counted" in out and "mine" in out.split("weren't counted")[0]
    assert scans.RECHARGE in out
    assert sidecar["stock"][-1]["centres"] == "20,0;0,-30;0,0"
    assert book.used(STEVE, 7) == scans.SCANS_PER_DAY


async def test_a_place_in_another_dimension_is_far_and_counted_there(world, book, sidecar) -> None:
    world["places"] = [_place("nether base", 3, 3, "nether")]
    sidecar["chests"]["nether 3,3"] = 7
    out = await ask(book, {"goal": 1})
    assert "7 in chests near nether base" in out and book.used(STEVE, 7) == 1
    assert [p["dim"] for p in sidecar["stock"]] == ["overworld", "nether"]


async def test_a_down_server_counts_nothing_and_refunds_the_scan(world, book, sidecar) -> None:
    world["places"].append(_place("mine", 500, 0))
    sidecar["down"].add("/map/stock")
    out = await ask(book, {"goal": 1})
    assert "can't answer right now" in out and book.used(STEVE, 7) == 0


async def test_no_saved_places_says_so_and_counts_around_them(world, book, sidecar) -> None:
    world["places"] = []
    sidecar["chests"]["overworld 0,0"] = 2
    out = await ask(book, {"goal": 1})
    assert "7 of 23" in out and "2 in chests near you, 5 on you" in out
    assert "no saved places in this world" in out and "mc_place_save" in out
    assert sidecar["stock"][-1]["centres"] == "0,0"


async def test_carried_shulker_boxes_are_said_to_be_unread(world, book, sidecar) -> None:
    sidecar["pocket"] = sidecar["pocket"].replace("shulker boxes=0", "shulker boxes=2")
    out = await ask(book, {"goal": 1})
    assert "They carry 2 shulker box(es)" in out


async def test_in_game_only_the_askers_own_pockets(world, book, sidecar) -> None:
    out = await ask(book, {"goal": 1, "gamertag": "Mira"})
    assert out.result_brief == "refused" and sidecar["sent"] == []
    same = await ask(book, {"goal": 1, "gamertag": "steve 42"})
    assert "17 of 23" in same


async def test_goals_that_count_nothing_or_are_unclear_say_so(world, book, sidecar) -> None:
    world["goals"] = [_goal(1, "Beacon")]
    assert "doesn't count an item" in await ask(book, {"goal": 1})
    assert "no open goal that counts" in await ask(book, {})
    assert "no goal 9" in await ask(book, {"goal": 9})
    world["goals"] = [
        _goal(1, "Get 23 iron blocks", "iron_block", 23),
        _goal(2, "Get 64 oak logs", "oak_log", 64),
    ]
    which = await ask(book, {})
    assert "Which goal?" in which and "oak_log ×64" in which
    assert sidecar["sent"] == []


# --- progress, in the app ---------------------------------------------------------------


async def test_the_app_counts_chests_and_only_names_the_goals_own_player(
    world, book, sidecar
) -> None:
    tools = _tools(book)
    world["places"].append(_place("mine", 500, 0))
    out = await tools["mc_goal_progress"]({"goal": 1}, PWA)
    assert "12 of 23" in out and "What they carry wasn't counted" in out
    assert sidecar["carry"] == [] and DAY not in sidecar["sent"]  # unlimited
    assert sidecar["stock"][-1]["centres"] == "20,0;0,-30;500,0"
    named = await tools["mc_goal_progress"]({"goal": 1, "gamertag": "Steve 42"}, PWA)
    assert "17 of 23" in named and sidecar["carry"][-1]["player"] == "Steve 42"
    # Where they stand comes from the pack's own answer.
    assert sidecar["stock"][-1]["centres"].endswith(";0,0")
    carried, stocked = len(sidecar["carry"]), len(sidecar["stock"])
    other = await tools["mc_goal_progress"]({"goal": 1, "gamertag": "Mira"}, PWA)
    assert other.result_brief == "refused"
    assert (len(sidecar["carry"]), len(sidecar["stock"])) == (carried, stocked)
    assert "isn't a gamertag" in await tools["mc_goal_progress"](
        {"goal": 1, "gamertag": "no;pe"}, PWA
    )
    sidecar["pocket"] = "[x INFO] [jbrain] carry refused: Steve 42 isn't online"
    offline = await tools["mc_goal_progress"]({"goal": 1, "gamertag": "Steve 42"}, PWA)
    assert "12 of 23" in offline and "isn't online" in offline
    sidecar["pocket"] = "[x INFO] something else"
    silent = await tools["mc_goal_progress"]({"goal": 1, "gamertag": "Steve 42"}, PWA)
    assert "the jbrain pack didn't answer" in silent


async def test_the_app_with_nothing_to_count_says_so(world, book, sidecar) -> None:
    world["places"] = []
    out = await _tools(book)["mc_goal_progress"]({"goal": 1}, PWA)
    assert out.result_brief == "nothing counted" and "no saved places" in out
    assert sidecar["stock"] == []


async def test_stale_files_say_how_old(world, book, sidecar) -> None:
    sidecar["age_s"] = 9 * 60
    out = await ask(book, {"goal": 1})
    assert "(as of 9 minutes ago)" in out


def test_it_is_registered_for_both_dave_tiers() -> None:
    from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS, MINECRAFT_DAVE_TOOLS
    from jbrain.agent.readtools import OPTIONAL_MINECRAFT_TOOLS
    from jbrain.api.panel_labels import STEP_LABELS

    assert "mc_goal_progress" in MINECRAFT_DAVE_TOOLS & MINECRAFT_DAVE_INGAME_TOOLS
    assert "mc_goal_progress" in OPTIONAL_MINECRAFT_TOOLS
    assert STEP_LABELS["mc_goal_progress"] == "Counted toward a goal"
