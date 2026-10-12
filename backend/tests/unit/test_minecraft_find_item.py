"""`mc_find_item` (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M23): chest contents from the
world's files and items on the ground from the pack, through the real handler, with the
sidecar faked and the deep-scan ledger in memory.

The contract: in game a search within 48 blocks of the asker is the Tricorder's free
short-range scan (files plus the live ground look); wider, or the whole dimension, is a
deep scan; the owner's app chats are unlimited; each hit is a line the Tricorder can be
aimed from (`first_hit`), the nearest first; and stale files are said to be stale.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecrafttools as mt
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.unit.test_minecraft_scans import MemoryBook, MemoryTargets

OWNER = SessionContext(principal_kind="owner")
PWA = ToolContext(session=OWNER, scopes=(), agent_session_id="pwa")
DOOR = ToolContext(session=OWNER, scopes=(), agent_session_id="door")
STEVE = Asker(key="111", gamertag="Steve 42", world="slot2", at=Spot(0, 64, 0, "overworld"))
DAY = "time query day"
CHESTS = {
    "searched": ["minecraft:diamond"],
    "in_no_container": [],
    "suggestions": [],
    "matched": 3,
    "totals": {"minecraft:diamond": 43},
    "found": [
        {
            "kind": "chest",
            "x": 20,
            "y": 64,
            "z": 5,
            "distance": 21,
            "items": {"minecraft:diamond": 42},
            "in_shulker_boxes": {"minecraft:diamond": 30},
            "name": "Loot at x=1 y=2 z=3, ",
        },
        {
            "kind": "item frame",
            "x": 40,
            "y": 70,
            "z": 0,
            "distance": 40,
            "items": {"minecraft:diamond": 1},
            "in_shulker_boxes": {},
            "name": "",
        },
    ],
    "unopened_loot": 2,
    "age_s": 30,
}
GROUND = [
    "[x INFO] [jbrain] items ground: 3 minecraft:diamond at x=4 y=63 z=-2,"
    " 5 blocks from Steve 42 (overworld)",
]


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "sent": [],
        "params": [],
        "replies": {
            DAY: ["[x INFO] Day is 7"],
            "/worlds": {"active": "slot2", "slots": [{"id": "slot2", "active": True}]},
            "/map/items": CHESTS,
        },
        "ground": GROUND,
        "down": set(),
    }

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        what = kw["json"]["command"] if path == "/command" else path
        if path in state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        state["sent"].append(what)
        if path == "/map/items":
            state["params"].append(kw["params"])
        if path == "/command":
            if what.startswith("scriptevent jb:items "):
                state["asked"] = json.loads(what.removeprefix("scriptevent jb:items "))
                return {"lines": state["ground"]}
            return {"lines": state["replies"].get(what, ["[x INFO] ok"])}
        return state["replies"].get(path, {})

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


async def ask(book: MemoryBook, who: Asker, args: dict[str, Any]) -> Any:
    with asking("door", who):
        return await _tools(book)["mc_find_item"](args, DOOR)


async def test_a_near_search_is_free_and_looks_on_the_ground_too(book, sidecar) -> None:
    out = await ask(book, STEVE, {"items": ["Diamond"], "radius": 48})
    assert isinstance(out, ToolOutput)
    assert "deep scan" not in out and book.used(STEVE, 7) == 0
    assert DAY not in sidecar["sent"]  # not a scan: the day isn't even asked
    params = sidecar["params"][-1]
    assert params["radius"] == 48 and params["items"] == "diamond" and params["flush"] == 1
    assert (params["x"], params["z"]) == (0, 0)
    assert sidecar["asked"] == {"player": "Steve 42", "types": ["diamond"], "max": 48}
    # The ground pile is nearer than any chest, so it leads and the Tricorder aims there.
    assert out.index("On the ground") < out.index("in a chest")
    assert mt.first_hit(out, "overworld") == Spot(4, 63, -2, "overworld")
    assert "- 3 diamond on the ground at x=4 y=63 z=-2, 5 blocks away" in out
    assert "- 42 diamond in a chest at x=20 y=64 z=5, 21 blocks away (30 of them" in out
    assert "In all, 43 diamond in 3 container(s) (1 more not listed)." in out
    assert "2 loot chest(s)" in out
    assert out.result_brief == "2 container(s), 1 on the ground"
    assert "as of" not in out  # 30 s old is fresh


async def test_the_player_written_name_is_fenced_and_never_aims_the_tricorder(
    book, sidecar
) -> None:
    sidecar["ground"] = ["[x INFO] [jbrain] items none: no minecraft:diamond on the ground"]
    out = await ask(book, STEVE, {"items": ["diamond"], "radius": 30})
    assert mt.first_hit(out, "overworld") == Spot(20, 64, 5, "overworld")
    assert "Loot at x=1" in out.split("Names players gave those containers:")[1]
    assert "<untrusted" in out or "minecraft-players" in out
    assert "On the ground within 48 blocks of the player" in out and ": none" in out


async def test_a_wide_search_in_game_is_a_deep_scan(book, sidecar) -> None:
    out = await ask(book, STEVE, {"items": ["diamond"]})
    assert "That used a deep scan: 1 scan left today" in out
    assert "radius" not in sidecar["params"][-1]
    assert "anywhere in the dimension" in out
    assert book.used(STEVE, 7) == 1
    # The same search from about the same place is free.
    nearby = replace(STEVE, at=Spot(10, 64, 10, "overworld"))
    again = await ask(book, nearby, {"items": ["diamond"]})
    assert again.result_brief == "repeat, free" and book.used(STEVE, 7) == 1
    # Past 48 blocks is wider than the Tricorder reaches: a scan.
    await ask(book, STEVE, {"items": ["diamond"], "radius": 49})
    assert book.used(STEVE, 7) == 2


async def test_a_near_radius_somewhere_else_is_still_a_deep_scan(book, sidecar) -> None:
    await ask(book, STEVE, {"items": ["diamond"], "radius": 20, "x": 500, "z": 500})
    assert book.used(STEVE, 7) == 1
    assert (sidecar["params"][-1]["x"], sidecar["params"][-1]["z"]) == (500, 500)


async def test_a_search_centred_elsewhere_leads_with_its_containers(book, sidecar) -> None:
    """Ground hits are measured from the player and container hits from the search's
    centre, so only the short-range scan (both from the player) orders them by distance
    (#1695): a deep scan at x=500 z=500 leads with the chests found there, not with a pile
    at the asker's feet."""
    out = await ask(book, STEVE, {"items": ["diamond"], "radius": 20, "x": 500, "z": 500})
    assert out.index("in a chest") < out.index("On the ground")
    assert mt.first_hit(out, "overworld") == Spot(20, 64, 5, "overworld")
    out = await ask(book, STEVE, {"items": ["diamond"]})
    assert out.index("in a chest") < out.index("On the ground")


async def test_an_id_in_no_container_costs_no_scan_and_suggests(book, sidecar) -> None:
    sidecar["replies"]["/map/items"] = {
        **CHESTS,
        "searched": ["minecraft:diamonds"],
        "in_no_container": ["minecraft:diamonds"],
        "suggestions": ["minecraft:diamond"],
        "found": [],
        "matched": 0,
        "totals": {},
        "unopened_loot": 0,
    }
    sidecar["ground"] = ["[x INFO] [jbrain] items none: nothing"]
    out = await ask(book, STEVE, {"items": ["diamonds"]})
    assert "Item ids that are in containers: minecraft:diamond." in out
    assert book.used(STEVE, 7) == 0  # refunded: nothing was really searched


async def test_the_app_is_unlimited_and_files_only_unless_a_player_is_named(book, sidecar) -> None:
    tools = _tools(book)
    out = await tools["mc_find_item"]({"items": ["diamond"], "x": 100, "z": -40}, PWA)
    assert "in a chest at x=20" in out and "deep scan" not in out
    assert not any(s.startswith("scriptevent") for s in sidecar["sent"])
    named = await tools["mc_find_item"](
        {"items": ["diamond"], "x": 0, "z": 0, "gamertag": "Steve 42"}, PWA
    )
    assert "On the ground within 48 blocks of the player" in named
    sidecar["ground"] = ["[x INFO] [jbrain] items refused: Steve 42 isn't online"]
    offline = await tools["mc_find_item"](
        {"items": ["diamond"], "x": 0, "z": 0, "gamertag": "Steve 42"}, PWA
    )
    assert "Nothing was looked for on the ground" in offline and "in a chest" in offline


async def test_stale_files_say_how_old(book, sidecar) -> None:
    sidecar["replies"]["/map/items"] = {**CHESTS, "age_s": 7 * 60 + 5}
    out = await ask(book, STEVE, {"items": ["diamond"], "radius": 48})
    assert "(as of 7 minutes ago)" in out
    assert mt.as_of(119) == "" and mt.as_of(None) == ""
    assert mt.as_of(3 * 3600) == " (as of 3 hours ago)"
    assert mt.as_of(5 * 86400) == " (as of 5 days ago)"


async def test_bad_arguments_are_refused_before_any_search(book, sidecar) -> None:
    tools = _tools(book)
    for bad in (
        {"items": []},
        {"items": ["diamond; op @a"]},
        {"items": [f"item{i}" for i in range(9)]},
        {"items": ["diamond"], "radius": "far"},
        {"items": ["diamond"], "dimension": "moon"},
        {"items": ["diamond"], "x": 1},
        {"items": ["diamond"], "x": 0, "z": 0, "gamertag": "no;pe"},
    ):
        out = await tools["mc_find_item"](bad, PWA)
        assert "/map/items" not in sidecar["sent"], bad
        assert isinstance(out, str)
    refused = await ask(book, STEVE, {"items": ["diamond"], "gamertag": "Mira"})
    assert refused.result_brief == "refused"


async def test_a_down_server_searched_nothing_and_costs_nothing(book, sidecar) -> None:
    sidecar["down"].add("/map/items")
    out = await ask(book, STEVE, {"items": ["diamond"]})
    assert "can't answer right now" in out and book.used(STEVE, 7) == 0
    sidecar["down"].clear()
    sidecar["ground"] = ["[x INFO] something else entirely"]
    out = await ask(book, STEVE, {"items": ["diamond"], "radius": 10})
    assert "the jbrain pack didn't answer" in out


def test_it_is_registered_for_both_dave_tiers() -> None:
    from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS, MINECRAFT_DAVE_TOOLS
    from jbrain.agent.readtools import OPTIONAL_MINECRAFT_TOOLS
    from jbrain.api.panel_labels import STEP_LABELS

    assert "mc_find_item" in MINECRAFT_DAVE_TOOLS & MINECRAFT_DAVE_INGAME_TOOLS
    assert "mc_find_item" in OPTIONAL_MINECRAFT_TOOLS
    assert STEP_LABELS["mc_find_item"] == "Looked for items"
