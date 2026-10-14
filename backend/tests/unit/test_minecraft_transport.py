"""Transport on request (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M10): `mc_transport`'s
guards that need no database, its console protocol with the jbrain pack, the door
offering it only to a player who has made a Power Pack (§M19), the guide's entry behind
the world's switch, and the switch's owner and debug routes. The database paths (the
asker's own places, the visited dimensions, the trip log) are in
`tests/integration/test_minecraft_transport_pg.py`."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jbrain.agent import minecrafttransport as mt
from jbrain.agent.agents import AGENTS, MINECRAFT_DAVE_INGAME_TOOLS, MINECRAFT_DAVE_TOOLS
from jbrain.agent.loop import ToolContext
from jbrain.api import debug_minecraft as debug
from jbrain.db.session import SessionContext
from jbrain.minecraft import client as mc
from jbrain.minecraft import guide
from jbrain.minecraft.asker import Asker, Spot, asking
from tests.unit.test_debug_minecraft import PRINCIPAL, FakeSupervisor, _request
from tests.unit.test_debug_minecraft import SETTINGS as DEBUG_SETTINGS
from tests.unit.test_minecraft_door import FakeExecutor, _door, _event, world  # noqa: F401
from tests.unit.test_minecraft_worlds_api import client, crossed, sidecar  # noqa: F401

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id="s-1")


def _tool(sent: list[Any]) -> Any:
    async def call(_cfg: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        sent.append((method, path))
        return {}

    return call


async def test_only_in_game_and_only_once_the_pack_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(mc, "call", _tool(sent))
    run = mt.build_minecraft_transport_handlers(object(), object())["mc_transport"]  # type: ignore[arg-type]
    app = await run({"place": "market"}, CTX)
    assert "only asked for in game" in str(app) and "mc_set_waypoint" in str(app)
    fresh = Asker(key="1", gamertag="Mira", world="slot2", at=Spot(0, 64, 0, "overworld"))
    with asking("s-1", fresh):
        locked = await run({"place": "market"}, CTX)
    # Canon (§M19): nothing names the pack to a player who can't make one yet.
    assert "Power Pack" not in str(locked) and "mc_set_waypoint" in str(locked)
    made = Asker(
        key="1",
        gamertag="Mira",
        world="slot2",
        at=Spot(0, 64, 0, "overworld"),
        known=frozenset({"tricorder", "power_pack_uses"}),
    )
    with asking("s-1", made):
        bad = await run({"place": "  "}, CTX)
    assert "saved places" in str(bad)
    assert sent == []  # refused before the box or the database was asked anything


def test_the_pack_lines_are_read_for_this_trip_only() -> None:
    line = '[2026 INFO] [Scripting] [jbrain] transport 0a1b2c3d moved {"left": 2}'
    assert mt.parse_line(line, "0a1b2c3d") == ("moved", {"left": 2})
    assert mt.parse_line(line, "ffffffff") is None
    assert mt.parse_line("[jbrain] transport 0a1b2c3d moved {broken", "0a1b2c3d") is None
    assert mt.parse_line("[jbrain] transport refused: SyntaxError", "0a1b2c3d") is None
    assert mt.parse_line('[jbrain] {"ev": "death"}', "0a1b2c3d") is None


def test_dave_holds_it_in_game_and_the_prompt_names_no_item() -> None:
    assert "mc_transport" in MINECRAFT_DAVE_TOOLS and "mc_transport" in MINECRAFT_DAVE_INGAME_TOOLS
    prompt = AGENTS["minecraft_dave"].prompt
    assert "`mc_transport`" in prompt
    head = prompt.split("**The Old Makers' items**")[0]
    for item in ("Power Pack", "Ender Pearl", "Blaze Powder"):
        assert item not in head, item


async def test_the_door_offers_transport_only_to_a_player_who_has_made_a_pack(
    world,  # noqa: F811
) -> None:
    executor = FakeExecutor()
    door = _door(executor)
    world["events"] = [_event(1, "take me to market", known=["power_pack"])]
    await door.tick()
    await door.drain()
    profile = executor.calls[-1]["profile"]
    assert "mc_transport" not in profile.tools
    assert "transport beacon" not in profile.prompt.split("## The Old Makers' items")[1]

    world["events"].append(_event(2, "take me to market", known=["power_pack_uses"]))
    await door.tick()
    await door.drain()
    profile = executor.calls[-1]["profile"]
    assert "mc_transport" in profile.tools
    # The world's teleport switch reads off when the slot doesn't say: no entry.
    assert "transport to your saved places" not in profile.prompt

    world["worlds"] = {"slots": [{"folder": "slot2", "power_pack_transport": True}]}
    world["events"].append(_event(3, "take me to market", known=["power_pack_uses"]))
    await door.tick()
    await door.drain()
    prompt = executor.calls[-1]["profile"].prompt
    assert "transport to your saved places" in prompt and "transport beacons" in prompt
    assert "keeping your things" not in prompt and "Nothing in this world yet" not in prompt


def test_the_guide_describes_transport_only_where_it_is_switched_on() -> None:
    made = {"power_pack_uses"}
    assert guide.entries_for(made, keep_switch=False, transport_switch=True) == [
        "tricorder",
        "power_pack",
        "power_pack_transport",
    ]
    assert guide.entries_for(made, keep_switch=True, transport_switch=True)[-2:] == [
        "power_pack_keep",
        "power_pack_transport",
    ]
    assert guide.entries_for(made, keep_switch=False)[-1] == "power_pack_unused"
    entry = guide.ENTRIES["power_pack_transport"]
    assert "only their own saved places" in entry and "Yes / No" in entry
    assert "transport beacon" in entry  # the first-Power-Pack beat's reveal (canon)
    assert "power_pack_transport" not in guide.entries_for({"power_pack"}, keep_switch=True)
    assert "transport to your saved places" in guide.for_owner()


def test_the_owners_switch_reaches_the_box_and_is_checked_first(
    client,  # noqa: F811
    sidecar,  # noqa: F811
) -> None:
    seen, replies = sidecar
    replies["/worlds/slot2/power-pack-transport"] = httpx.Response(
        200, json={"id": "slot2", "power_pack_transport": False, "applies": "now"}
    )
    resp = client.put("/api/minecraft/worlds/slot2/power-pack-transport", json={"on": False})
    assert resp.status_code == 200 and resp.json()["power_pack_transport"] is False
    assert (seen[-1].method, seen[-1].url.path) == ("POST", "/worlds/slot2/power-pack-transport")
    assert json.loads(seen[-1].content) == {"on": False}
    count = len(seen)
    bad = client.put("/api/minecraft/worlds/slot2/power-pack-transport", json={})
    assert bad.status_code == 422
    wrong = client.put("/api/minecraft/worlds/../power-pack-transport", json={"on": True})
    assert wrong.status_code in (404, 422)
    assert len(seen) == count


async def test_the_debug_switch_passes_through_and_is_recorded(sidecar) -> None:  # noqa: F811
    seen, replies = sidecar
    replies["/worlds/slot2/power-pack-transport"] = httpx.Response(
        200, json={"id": "slot2", "power_pack_transport": True, "applies": "next load"}
    )
    req = _request(FakeSupervisor([]))
    out = await debug.minecraft_power_pack_transport(
        "slot2", debug.PowerPackIn(on=True), req, DEBUG_SETTINGS, PRINCIPAL
    )
    assert out["power_pack_transport"] is True
    assert seen[-1].url.path == "/worlds/slot2/power-pack-transport"
    assert req.state.debug_detail == "minecraft power pack transport slot2 on=True"


def test_a_new_world_takes_the_switch_and_leaves_it_to_the_box_when_unset(
    client,  # noqa: F811
    sidecar,  # noqa: F811
) -> None:
    seen, _replies = sidecar
    resp = client.post(
        "/api/minecraft/worlds/slot3/create", json={"name": "Quiet", "power_pack_transport": False}
    )
    assert resp.status_code == 200
    assert json.loads(seen[-1].content) == {"name": "Quiet", "power_pack_transport": False}
    client.post("/api/minecraft/worlds/slot3/create", json={"name": "Usual"})
    assert "power_pack_transport" not in json.loads(seen[-1].content)
    count = len(seen)
    bad = client.post("/api/minecraft/worlds/slot3/create", json={"power_pack_transport": "maybe"})
    assert bad.status_code == 422 and len(seen) == count
    # A rename or setting change never carries it: the switch has its own route.
    client.patch("/api/minecraft/worlds/slot3", json={"power_pack_transport": False})
    assert "power_pack_transport" not in json.loads(seen[-1].content)
