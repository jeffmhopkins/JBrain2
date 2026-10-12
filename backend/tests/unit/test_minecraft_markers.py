"""`mc_gates_beacons`' pure parts and the covered rule (docs/plans/MINECRAFT_BEDROCK_PLAN.md
§M26): what a gate and a beacon say, which saved places a beacon protects, and that in game
nothing the asker hasn't unlocked is named. The rows, the drain and the handler against the
database are in `tests/integration/test_mc_world_markers_pg.py`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent import minecraftmarkers as mk
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft import markers
from jbrain.minecraft.asker import Asker, Spot, asking

CTX = ToolContext(
    session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id="door-1"
)

TREE_FARM = {"place": "Tree farm", "dim": "overworld", "x": -160, "y": 68, "z": -140}
GATE_1 = {
    "id": 1,
    "dim": "overworld",
    "x": 28,
    "y": 71,
    "z": 14,
    "axis": "x",
    "planter": "BlockyFox",
    "state": "open",
    "target": TREE_FARM,
}
GATE_2 = {**GATE_1, "id": 2, "planter": "Mira_P", "state": "dormant", "target": None}
GATE_3 = {
    **GATE_1,
    "id": 3,
    "target": {"place": "Nether base", "dim": "nether", "x": -20, "y": 72, "z": 40},
}
BEACON = {"dim": "overworld", "x": 60, "y": 72, "z": -40, "placed_by": "Mira_P"}
BEACON_2 = {"dim": "overworld", "x": 90, "y": 74, "z": -10, "placed_by": "BlockyFox"}
GOT = {"beacons": [BEACON, BEACON_2], "gates": [GATE_1, GATE_2, GATE_3], "as_of": 1_791_640_000}


def place(name: str, x: int, z: int, dim: str = "overworld") -> Any:
    return SimpleNamespace(name=name, x=x, y=64, z=z, dim=dim)


def test_a_beacon_covers_its_chunk_and_one_on_each_side_and_nothing_else() -> None:
    # X 60 is chunk 3, Z -40 chunk -3: X 32..79 and Z -64..-17 are covered.
    assert markers.area_of(BEACON) == (32, -64, 79, -17)
    for x, z in ((32, -64), (79, -17), (60, -40)):
        assert markers.covers(BEACON, "overworld", x, z), (x, z)
    for x, z in ((31, -40), (80, -40), (60, -65), (60, -16)):
        assert not markers.covers(BEACON, "overworld", x, z), (x, z)
    assert not markers.covers(BEACON, "nether", 60, -40)
    # Negative coordinates floor into the right chunk, as the pack's Math.floor does.
    assert markers.chunk_of(-1) == -1 and markers.chunk_of(-16) == -1
    assert markers.chunk_of(-17) == -2


def test_a_gate_says_where_it_leads_or_that_it_is_dormant() -> None:
    assert mk.gate_center(GATE_1) == (30.0, 14.5)
    assert mk.gate_center({**GATE_1, "axis": "z"}) == (28.5, 16.0)
    line = mk.gate_line(GATE_1, whose=True, asker=None)
    assert line.startswith("Gate 1 (BlockyFox's) at X 30, Y 71, Z 14 in the Overworld")
    assert "open, leads to Tree farm at X -160, Y 68, Z -140, 244 blocks north-west" in line
    assert "dormant: not open yet" in mk.gate_line(GATE_2, whose=True, asker=None)
    nether = mk.gate_line(GATE_3, whose=False, asker=None)
    assert "(BlockyFox's)" not in nether
    assert nether.endswith("leads to Nether base at X -20, Y 72, Z 40 in the Nether.")


def test_the_owners_listing_is_everyones_with_their_places_checked() -> None:
    body, brief = mk.listing(
        GOT,
        who=None,
        asker=None,
        show_beacons=True,
        show_gates=True,
        places=[place("home", 70, -30), place("mine", 500, 500)],
        place_owner="BlockyFox",
    )
    assert brief == "3 gates · 2 beacons"
    assert "Gate 2 (Mira_P's)" in body
    assert "No-Spawn Beacon (Mira_P's) at X 60, Y 72, Z -40" in body
    assert "protects X 32 to 79, Z -64 to -17 (48 × 48 blocks, floor to sky)" in body
    assert "overlaps 1 other beacon" in body
    assert "- home: protected, by Mira_P's beacon at X 60, Z -40." in body
    assert "- mine: not protected by any beacon." in body
    assert "As of the game's last report: 2026-10-" in body


def test_one_players_listing_is_only_theirs() -> None:
    body, brief = mk.listing(
        GOT,
        who="blockyfox",
        asker=None,
        show_beacons=True,
        show_gates=True,
        places=[],
        place_owner=None,
    )
    assert brief == "2 gates · 1 beacon"
    assert "Gate 2" not in body and "X 60, Y 72" not in body


def test_in_game_only_the_askers_own_and_only_what_they_have_unlocked() -> None:
    asker = Asker("111", "BlockyFox", "w", Spot(30, 70, 30, "overworld"))
    body, brief = mk.listing(
        GOT,
        who="BlockyFox",
        asker=asker,
        show_beacons=False,
        show_gates=True,
        places=[place("home", 70, -30)],
        place_owner="BlockyFox",
    )
    assert brief == "2 gates"
    assert "Beacon" not in body and "protected" not in body  # not unlocked: not named
    assert "Gate 1 at X 30" in body and "(BlockyFox's)" not in body
    assert "From you:" in body
    # With beacons unlocked, a place covered by someone else's beacon doesn't say where it is.
    body, _ = mk.listing(
        GOT,
        who="BlockyFox",
        asker=asker,
        show_beacons=True,
        show_gates=False,
        places=[place("home", 70, -30), place("barn", 95, -5)],
        place_owner="BlockyFox",
    )
    assert "Gate" not in body
    assert "- home: protected by a No-Spawn Beacon." in body
    assert "- barn: protected, by their beacon at X 90, Z -10." in body


def test_in_game_beacons_with_no_recorded_placer_are_counted_not_dropped() -> None:
    # Beacons placed before pack 1.10 have no placer: their owner must still hear of them.
    old = {"dim": "overworld", "x": 200, "y": 70, "z": 200, "placed_by": ""}
    got = {**GOT, "beacons": [BEACON, old, {**old, "x": 300}]}
    asker = Asker("222", "Steve", "w", Spot(0, 64, 0, "overworld"))
    body, brief = mk.listing(
        got,
        who="Steve",
        asker=asker,
        show_beacons=True,
        show_gates=False,
        places=[],
        place_owner="Steve",
    )
    assert "No-Spawn Beacons: none placed by Steve." in body
    assert "Also 2 beacons placed before Dave kept track of who placed them" in body
    assert "X 200" not in body and "X 300" not in body
    assert brief == "0 beacons"


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"sent": [], "down": False, "worlds": {"slots": []}}

    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        state["sent"].append(path)
        if state["down"]:
            raise HTTPException(status_code=503, detail="the server is stopped")
        return state["worlds"]

    monkeypatch.setattr(mc, "call", call)
    return state


def _tool() -> Any:
    return mk.build_minecraft_markers_handlers(object(), object(), None)["mc_gates_beacons"]  # type: ignore[arg-type]


async def test_in_game_with_neither_unlocked_it_names_nothing(sidecar) -> None:
    asker = Asker("111", "Steve", "w", Spot(0, 64, 0, "overworld"))
    with asking("door-1", asker):
        out = await _tool()({}, CTX)
    assert out.result_brief == "not unlocked"
    assert "gate" not in str(out).lower() and "beacon" not in str(out).lower()
    assert sidecar["sent"] == []


async def test_in_the_app_a_bad_slot_or_no_world_is_said_plainly(sidecar) -> None:
    app = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    assert "slot must be" in await _tool()({"slot": 9}, app)
    assert sidecar["sent"] == []
    assert "No world is loaded" in await _tool()({}, app)
    assert "Slot 2 is empty" in await _tool()({"slot": 2}, app)
    sidecar["down"] = True
    assert "can't answer right now" in await _tool()({}, app)
