"""`mc_find_villager`, `mc_find_pet` and the Tricorder's portal pointer
(docs/plans/MINECRAFT_BEDROCK_PLAN.md §M4e), through the real handlers with the sidecar
faked and the deep-scan ledger in memory.

The owner's decisions (2026-10-13) pinned here:
1. Anyone's pets can be found in game, not only the asker's own, and any named mob.
2. Only the trades a villager offers now are told (the sidecar drops locked ones; the
   tool says so and never asks for more).
3. Everything follows the chest rule, the asker's own pets included: within 48 blocks of
   the asker is free, farther is a deep scan, a repeat from about the same place free.
   The app is unlimited.
4. The lore's two lines are the shape the answer asks Dave to speak in.
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
from jbrain.minecraft import door as door_mod
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.unit.test_minecraft_scans import MemoryBook, MemoryTargets

OWNER = SessionContext(principal_kind="owner")
PWA = ToolContext(session=OWNER, scopes=(), agent_session_id="pwa")
DOOR = ToolContext(session=OWNER, scopes=(), agent_session_id="door")
HANK = Asker(
    key="111",
    gamertag="Hank",
    world="slot2",
    at=Spot(0, 64, 0, "overworld"),
    pid="-4294967295",
)
DAY = "time query day"

LIBRARIAN = {
    "kind": "villager_v2",
    "job": "librarian",
    "level": 3,
    "level_name": "Expert",
    "locked_in": True,
    "name": "",
    "x": -230,
    "y": 64,
    "z": 0,
    "distance": 230,
    "trades": [
        {
            "tier": 3,
            "buy": ["minecraft:emerald", 12],
            "buy2": ["minecraft:book", 1],
            "sell": ["minecraft:enchanted_book", 1],
            "enchantments": [["mending", 1]],
            "uses": 0,
            "max_uses": 12,
            "used_up": False,
        },
        {
            "tier": 0,
            "buy": ["minecraft:paper", 24],
            "buy2": None,
            "sell": ["minecraft:emerald", 1],
            "enchantments": [],
            "uses": 16,
            "max_uses": 16,
            "used_up": True,
        },
    ],
    "locked_trades": 2,
}
REX = {
    "kind": "wolf",
    "name": "Rex <b>",
    "id": "-8589934591",
    "tamed": True,
    "owner": "-4294967295",
    "owner_name": "Hank",
    "sitting": True,
    "baby": False,
    "coat": [],
    "dimension": "overworld",
    "x": 85,
    "y": 70,
    "z": -85,
    "distance": 120,
}


def _near(hit: dict[str, Any]) -> dict[str, Any]:
    return {**hit, "x": 10, "z": 10, "distance": 14}


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replies per path, each a list consumed in order (the last one repeats)."""
    state: dict[str, Any] = {
        "sent": [],
        "params": {},
        "replies": {
            DAY: ["[x INFO] Day is 7"],
            "/worlds": {"active": "slot2", "slots": [{"id": "slot2", "active": True}]},
        },
        "queue": {},
        "down": set(),
    }

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        what = kw["json"]["command"] if path == "/command" else path
        if path in state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        state["sent"].append(what)
        if "params" in kw:
            state["params"].setdefault(path, []).append(kw["params"])
        if path == "/command":
            if what.startswith("scriptevent jb:waypoint "):
                state["waypoint"] = json.loads(what.removeprefix("scriptevent jb:waypoint "))
                return {"lines": ["[x INFO] [jbrain] waypoint set for Hank: Base"]}
            return {"lines": state["replies"].get(what, ["[x INFO] ok"])}
        queue = state["queue"].get(path)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
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


async def ask(book: MemoryBook, who: Asker, tool: str, args: dict[str, Any]) -> Any:
    with asking("door", who):
        return await _tools(book)[tool](args, DOOR)


def found(*hits: dict[str, Any], age: int = 30) -> dict[str, Any]:
    return {"found": list(hits), "matched": len(hits), "age_s": age, "owner_known": True}


# --- villagers ---------------------------------------------------------------------


async def test_a_villager_within_48_blocks_is_free(book, sidecar) -> None:
    sidecar["queue"]["/map/villagers"] = [found(_near(LIBRARIAN))]
    out = await ask(book, HANK, "mc_find_villager", {"enchantment": "Mending"})
    assert isinstance(out, ToolOutput)
    assert book.used(HANK, 7) == 0 and DAY not in sidecar["sent"]
    (params,) = sidecar["params"]["/map/villagers"]
    assert params["radius"] == 48 and params["enchant"] == "mending" and params["flush"] == 1
    assert (params["x"], params["z"]) == (0, 0)
    assert "an Expert librarian at x=10 y=64 z=10, 14 blocks south-east" in out
    assert "an enchanted book (Mending I) for 12 emerald and 1 book" in out
    assert "short-range scan" in out and "free" in out
    # The lore's line (canon, "Villagers and pets"), as the shape to answer in.
    assert "From up here I see a librarian 230 blocks west who sells Mending for 12" in out
    assert out.result_brief == "1 villager(s)"


async def test_farther_is_a_deep_scan_and_a_repeat_is_free(book, sidecar) -> None:
    sidecar["queue"]["/map/villagers"] = [found(), found(LIBRARIAN)]
    out = await ask(book, HANK, "mc_find_villager", {"enchantment": "mending"})
    near, wide = sidecar["params"]["/map/villagers"]
    assert near["radius"] == 48 and "radius" not in wide
    assert "230 blocks west" in out and "anywhere in the dimension" in out
    assert "That used a deep scan: 1 scan left today" in out
    assert book.used(HANK, 7) == 1
    sidecar["queue"]["/map/villagers"] = [found()]
    again = await ask(
        book,
        replace(HANK, at=Spot(30, 64, 30, "overworld")),
        "mc_find_villager",
        {"enchantment": "mending"},
    )
    assert again.result_brief == "repeat, free" and book.used(HANK, 7) == 1


async def test_a_trade_used_up_says_it_waits_and_locked_ones_are_never_told(book, sidecar) -> None:
    sidecar["queue"]["/map/villagers"] = [found(_near(LIBRARIAN), age=600)]
    out = await ask(book, HANK, "mc_find_villager", {"profession": "librarian"})
    assert "an emerald for 24 paper — used up until it restocks" in out
    assert "(its job is locked in" in out
    assert "Only the trades each offers now" in out and "Dave never tells those" in out
    assert "(as of 10 minutes ago)" in out
    # The tool asks the files for nothing about tiers: the sidecar drops locked trades.
    assert not any("tier" in k for k in sidecar["params"]["/map/villagers"][0])


async def test_nothing_anywhere_is_given_back(book, sidecar) -> None:
    sidecar["queue"]["/map/villagers"] = [found()]
    out = await ask(book, HANK, "mc_find_villager", {"enchantment": "mending"})
    assert "none that match" in out and book.used(HANK, 7) == 0


async def test_out_of_scans_answers_with_the_recharge_line(book, sidecar) -> None:
    for _ in range(2):
        await book.reserve(HANK.world, HANK.key, HANK.gamertag, 7)
    sidecar["queue"]["/map/villagers"] = [found(), found(LIBRARIAN)]
    out = await ask(book, HANK, "mc_find_villager", {"enchantment": "mending"})
    assert "Out of deep scans" in out and "230 blocks" not in out
    assert len(sidecar["params"]["/map/villagers"]) == 1  # only the free near look


async def test_a_near_radius_never_scans_and_elsewhere_always_does(book, sidecar) -> None:
    sidecar["queue"]["/map/villagers"] = [found()]
    out = await ask(book, HANK, "mc_find_villager", {"profession": "cleric", "radius": 30})
    assert "within 30 blocks" in out and book.used(HANK, 7) == 0
    sidecar["queue"]["/map/villagers"] = [found(LIBRARIAN)]
    await ask(book, HANK, "mc_find_villager", {"profession": "cleric", "x": 900, "z": 0})
    assert book.used(HANK, 7) == 1
    assert sidecar["params"]["/map/villagers"][-1]["x"] == 900


async def test_the_app_is_unlimited_and_bad_words_are_refused(book, sidecar) -> None:
    sidecar["queue"]["/map/villagers"] = [found(LIBRARIAN)]
    out = await _tools(book)["mc_find_villager"]({"sells": "enchanted book", "x": 0, "z": 0}, PWA)
    (params,) = sidecar["params"]["/map/villagers"]
    assert "radius" not in params and params["sells"] == "enchanted_book"
    assert "deep scan" not in out and "From up here" not in out
    for bad in ({"profession": "a;b"}, {"enchantment": "x" * 50}, {"limit": "many"}):
        assert isinstance(await _tools(book)["mc_find_villager"](bad, PWA), str)


# --- pets --------------------------------------------------------------------------


async def test_my_dog_is_the_askers_own_by_their_id_and_costs_a_scan_far_away(
    book, sidecar
) -> None:
    sidecar["queue"]["/map/pets"] = [found(), found(REX)]
    out = await ask(book, HANK, "mc_find_pet", {"kind": "dog"})
    near, wide = sidecar["params"]["/map/pets"]
    assert near["owner"] == "-4294967295" and near["kind"] == "wolf"
    assert near["radius"] == 48 and wide.get("everywhere") == 1 and "radius" not in wide
    # Your own pets included (owner, 2026-10-13): farther than 48 is a deep scan.
    assert book.used(HANK, 7) == 1 and "That used a deep scan" in out
    assert "a wolf, sitting at x=85 y=70 z=-85, 120 blocks north-east" in out
    # Names and owners are player-written: fenced, never in the line itself.
    assert "Rex <b>" not in out.split("Their names")[0]
    assert "minecraft-players" in out.split("Their names")[1]
    assert "Rex is 120 blocks north-east, sitting right where you left him." in out


async def test_a_pet_within_48_blocks_is_free(book, sidecar) -> None:
    sidecar["queue"]["/map/pets"] = [found(_near(REX))]
    out = await ask(book, HANK, "mc_find_pet", {"name": "rex"})
    (params,) = sidecar["params"]["/map/pets"]
    assert params["name"] == "rex" and params["owner"] == "-4294967295"  # theirs first
    assert book.used(HANK, 7) == 0 and "14 blocks south-east" in out


async def test_anyones_pets_can_be_found_in_game(book, sidecar) -> None:
    sidecar["queue"]["/map/pets"] = [found(_near(REX))]
    await ask(book, HANK, "mc_find_pet", {"kind": "cat", "gamertag": "Mira"})
    assert sidecar["params"]["/map/pets"][-1]["owner_name"] == "Mira"
    await ask(book, HANK, "mc_find_pet", {"kind": "cat", "anyone": True})
    every = sidecar["params"]["/map/pets"][-1]
    assert "owner" not in every and "owner_name" not in every


async def test_an_owner_dave_hasnt_met_is_said_and_costs_nothing(book, sidecar) -> None:
    sidecar["queue"]["/map/pets"] = [{"found": [], "matched": 0, "owner_known": False}]
    out = await ask(book, HANK, "mc_find_pet", {"kind": "wolf", "gamertag": "Mira"})
    assert out.result_brief == "owner unknown" and book.used(HANK, 7) == 0
    assert "asks him something in game" in out
    assert len(sidecar["params"]["/map/pets"]) == 1


async def test_an_old_pack_without_an_id_falls_back_to_the_gamertag(book, sidecar) -> None:
    sidecar["queue"]["/map/pets"] = [found(_near(REX))]
    await ask(book, replace(HANK, pid=None), "mc_find_pet", {"kind": "wolf"})
    assert sidecar["params"]["/map/pets"][-1]["owner_name"] == "Hank"


async def test_pets_in_the_app_are_everyones_and_unlimited(book, sidecar) -> None:
    sidecar["queue"]["/map/pets"] = [found({**REX, "dimension": "nether", "distance": None})]
    out = await _tools(book)["mc_find_pet"]({"kind": "wolf", "x": 0, "z": 0}, PWA)
    (params,) = sidecar["params"]["/map/pets"]
    assert "owner" not in params and params["everywhere"] == 1
    assert "in the Nether" in out and "deep scan" not in out


# --- the Tricorder through a portal -------------------------------------------------


async def test_a_target_across_the_nether_carries_the_nearest_portals(book, sidecar) -> None:
    portals = [{"x": 8 * i, "y": 70, "z": 0, "dimension": "nether"} for i in range(12, 0, -1)]
    sidecar["replies"]["/map/portals"] = {"portals": portals}
    in_nether = replace(HANK, at=Spot(0, 64, 0, "nether"))
    out = await ask(
        book,
        in_nether,
        "mc_set_waypoint",
        {"x": 800, "z": 0, "dimension": "overworld", "label": "Base"},
    )
    assert sidecar["params"]["/map/portals"][-1] == {"slot": "slot2", "dim": "nether"}
    via = sidecar["waypoint"]["via"]
    assert len(via) == mt.VIA_MAX and via[0] == {"dim": "nether", "x": 8, "z": 0}
    assert [p["x"] for p in via] == sorted(p["x"] for p in via)
    assert "nearest of 8 known Nether portal(s)" in out


async def test_portals_near_where_the_target_maps_when_the_asker_is_not_there(
    book, sidecar
) -> None:
    sidecar["replies"]["/map/portals"] = {
        "portals": [{"x": 0, "z": 0}, {"x": 100, "z": 0}, {"x": "bad", "z": 0}]
    }
    await ask(book, HANK, "mc_set_waypoint", {"x": 800, "z": 0, "dimension": "overworld"})
    # The target's x 800 is x 100 in the Nether: that portal first.
    assert [p["x"] for p in sidecar["waypoint"]["via"]] == [100, 0]


async def test_no_portals_known_or_the_end_sends_no_via(book, sidecar) -> None:
    await ask(book, HANK, "mc_set_waypoint", {"x": 1, "z": 2, "dimension": "overworld"})
    assert "via" not in sidecar["waypoint"]
    await ask(book, HANK, "mc_set_waypoint", {"x": 1, "z": 2, "dimension": "the_end"})
    assert "via" not in sidecar["waypoint"]
    assert len(sidecar["params"].get("/map/portals", [])) == 1  # none asked for the End
    sidecar["down"].add("/map/portals")
    out = await ask(book, HANK, "mc_set_waypoint", {"x": 1, "z": 2, "dimension": "nether"})
    assert "via" not in sidecar["waypoint"] and "now points" in out


# --- the asker's id and the registries ------------------------------------------------


def test_the_door_takes_the_askers_id_only_as_a_number() -> None:
    base = {
        "kind": "dave",
        "id": 1,
        "name": "Hank",
        "text": "where's my dog",
        "x": 1,
        "y": 64,
        "z": 2,
        "dim": "overworld",
        "xuid": "111",
    }
    q = door_mod.parse_question({**base, "pid": "-4294967295"})
    assert q is not None and q.asker.pid == "-4294967295"
    for bad in ("12; op", 12, None, "1" * 30):
        q = door_mod.parse_question({**base, "pid": bad})
        assert q is not None and q.asker.pid is None


def test_trade_words() -> None:
    assert (
        mt.trade_text(
            {
                "sell": ["minecraft:enchanted_book", 1],
                "enchantments": [["silk_touch", 1], ["unbreaking", 3]],
                "buy": ["minecraft:emerald", 20],
                "buy2": None,
            }
        )
        == "an enchanted book (Silk Touch I, Unbreaking III) for 20 emerald"
    )
    assert (
        mt.trade_text({"sell": ["minecraft:emerald", 2], "buy": ["minecraft:paper", 24]})
        == "2 emerald for 24 paper"
    )
    assert mt.pet_kind("Dog") == "wolf" and mt.pet_kind("minecraft:cat") == "cat"


def test_both_are_registered_for_both_dave_tiers() -> None:
    from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS, MINECRAFT_DAVE_TOOLS
    from jbrain.agent.readtools import OPTIONAL_MINECRAFT_TOOLS
    from jbrain.api.panel_labels import STEP_LABELS

    for tool, label in (
        ("mc_find_villager", "Looked for villagers"),
        ("mc_find_pet", "Looked for pets"),
    ):
        assert tool in MINECRAFT_DAVE_TOOLS & MINECRAFT_DAVE_INGAME_TOOLS
        assert tool in OPTIONAL_MINECRAFT_TOOLS
        assert STEP_LABELS[tool] == label
