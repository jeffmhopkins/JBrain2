"""Minecraft_Dave's read of the gates and beacons: `mc_gates_beacons` (plan §M26, §M12,
§M16).

It answers "where's my gate?", "where does gate 2 go?" and "is my base protected?" from
the rows the session drain keeps (`minecraft/markers.py`), so it needs no round-trip to the
game and works for a world that isn't loaded, as of the pack's last report.

- **In game** it is the asker's own: their gates and beacons, and which of their saved
  places a beacon covers. It follows the item guide (§M19): what the asker hasn't unlocked
  isn't named at all — no gates before the Wormhole Seed, no beacons before the No-Spawn
  Beacon — and the door doesn't offer the tool until one of them is (`door.py`).
- **In the app** (the owner's) it is everyone's, or one player's with `player`, and the
  chat player's saved places are checked.

Gamertags and place names are player-authored, so the listing reaches the model fenced.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.minecraftmap import parse_slot
from jbrain.agent.minecrafttools import (
    _DIMENSION_NAMES,
    Player,
    _guarded,
    _places,
    bearing,
    chat_player_for,
    check_gamertag,
    fence,
    from_asker,
)
from jbrain.db.session import scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import markers
from jbrain.minecraft.asker import Asker, Spot, asker_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.settings_store import SqlSettingsStore

BEACONS = "no_spawn_beacon"
GATES = "wormhole_seed"


def _xyz(x: int, y: int | None, z: int) -> str:
    return f"X {x}" + (f", Y {y}" if y is not None else "") + f", Z {z}"


def gate_center(g: dict[str, Any]) -> tuple[float, float]:
    """The middle of the gate's 4-wide ring, from its lowest corner and axis."""
    if g["axis"] == "x":
        return g["x"] + 2.0, g["z"] + 0.5
    return g["x"] + 0.5, g["z"] + 2.0


def gate_line(g: dict[str, Any], *, whose: bool, asker: Asker | None) -> str:
    """One gate in words: where, whose, and where it leads (or that it's dormant)."""
    dim = _DIMENSION_NAMES[g["dim"]]
    owner = f" ({g['planter']}'s)" if whose and g["planter"] else ""
    # The middle of the ring, where a player stands to step through, as the map shows it.
    cx, cz = (math.floor(c) for c in gate_center(g))
    head = f"Gate {g['id']}{owner} at {_xyz(cx, g['y'], cz)} in the {dim}"
    head += from_asker(asker, Spot(cx, g["y"], cz, g["dim"]))
    t = g["target"]
    if g["state"] != "open" or t is None:
        return (
            f"{head} — dormant: not open yet. Its planter can ask you to open it to one of"
            " their saved places, standing within 4 blocks."
        )
    to = f"{t['place']} at {_xyz(t['x'], t['y'], t['z'])}"
    if t["dim"] != g["dim"]:
        return f"{head} — open, leads to {to} in the {_DIMENSION_NAMES[t['dim']]}."
    cx, cz = gate_center(g)
    way, far = bearing(cx, cz, t["x"] + 0.5, t["z"] + 0.5)
    return f"{head} — open, leads to {to}, {far} blocks {way} of the gate."


def beacon_line(b: dict[str, Any], *, whose: bool, asker: Asker | None, others: int) -> str:
    x0, z0, x1, z1 = markers.area_of(b)
    owner = f" ({b['placed_by']}'s)" if whose and b["placed_by"] else ""
    dim = _DIMENSION_NAMES[b["dim"]]
    line = (
        f"No-Spawn Beacon{owner} at {_xyz(b['x'], b['y'], b['z'])} in the {dim}"
        f"{from_asker(asker, Spot(b['x'], b['y'], b['z'], b['dim']))}"
        f" — protects X {x0} to {x1}, Z {z0} to {z1} (48 × 48 blocks, floor to sky)"
    )
    if others:
        line += f"; overlaps {others} other beacon{'s' if others > 1 else ''}"
    return line + "."


def covered_by(place: Any, beacons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in beacons if markers.covers(b, str(place.dim), place.x, place.z)]


def _overlaps(b: dict[str, Any], beacons: list[dict[str, Any]]) -> int:
    """How many other beacons' squares share a chunk with this one's."""
    return sum(
        1
        for o in beacons
        if o is not b
        and o["dim"] == b["dim"]
        and abs(markers.chunk_of(o["x"]) - markers.chunk_of(b["x"])) <= 2
        and abs(markers.chunk_of(o["z"]) - markers.chunk_of(b["z"])) <= 2
    )


def _mine(name: str, who: str) -> bool:
    return name.casefold() == who.casefold()


def listing(
    got: dict[str, Any],
    *,
    who: str | None,
    asker: Asker | None,
    show_beacons: bool,
    show_gates: bool,
    places: list[Any],
    place_owner: str | None,
) -> tuple[str, str]:
    """The answer's body and its brief. `who` narrows to one player's (in game always the
    asker); `places` are that player's saved places, checked against every beacon."""
    every_beacon = got["beacons"]
    beacons = [b for b in every_beacon if who is None or _mine(b["placed_by"], who)]
    gates = [g for g in got["gates"] if who is None or _mine(g["planter"], who)]
    whose = asker is None
    lines: list[str] = []
    brief: list[str] = []
    if show_gates:
        if gates:
            lines.append("Wormhole gates:")
            lines.extend(f"- {gate_line(g, whose=whose, asker=asker)}" for g in gates)
        else:
            lines.append("Wormhole gates: none" + (f" planted by {who}." if who else "."))
        brief.append(f"{len(gates)} gate{'s' if len(gates) != 1 else ''}")
    if show_beacons:
        if beacons:
            lines.append("No-Spawn Beacons:")
            lines.extend(
                f"- {beacon_line(b, whose=whose, asker=asker, others=_overlaps(b, every_beacon))}"
                for b in beacons
            )
        else:
            lines.append("No-Spawn Beacons: none" + (f" placed by {who}." if who else "."))
        brief.append(f"{len(beacons)} beacon{'s' if len(beacons) != 1 else ''}")
        if places:
            lines.append(f"{place_owner}'s saved places, protected or not:")
            for p in places:
                hits = covered_by(p, every_beacon)
                if not hits:
                    lines.append(f"- {p.name}: not protected by any beacon.")
                    continue
                b = hits[0]
                if whose:
                    by = f"{b['placed_by']}'s beacon" if b["placed_by"] else "a beacon"
                    lines.append(f"- {p.name}: protected, by {by} at X {b['x']}, Z {b['z']}.")
                elif who and _mine(b["placed_by"], who):
                    lines.append(
                        f"- {p.name}: protected, by their beacon at X {b['x']}, Z {b['z']}."
                    )
                else:
                    # Someone else's beacon: in game, its place is theirs to tell.
                    lines.append(f"- {p.name}: protected by a No-Spawn Beacon.")
    if got["as_of"]:
        when = datetime.fromtimestamp(got["as_of"], UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"As of the game's last report: {when}.")
    return "\n".join(lines), " · ".join(brief) or "nothing"


NOT_YET = ToolOutput(
    "There's nothing here this player can be told about yet. Answer from what you know.",
    result_brief="not unlocked",
)


def build_minecraft_markers_handlers(
    maker: async_sessionmaker[AsyncSession],
    config: Any,
    settings_store: SqlSettingsStore | None,
) -> dict[str, ToolHandler]:
    """`mc_gates_beacons`: the drain's rows under `ctx.session`, the sidecar only to name
    the world."""

    async def mc_gates_beacons(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        asker = asker_for(ctx.agent_session_id)
        if asker is not None:
            show_beacons, show_gates = BEACONS in asker.known, GATES in asker.known
            if not (show_beacons or show_gates):
                return NOT_YET
            world: str | None = asker.world
        else:
            show_beacons = show_gates = True
            slot_n = parse_slot(arguments.get("slot"))
            if isinstance(slot_n, str):
                return slot_n
            try:
                got_worlds = await mc.call(config, "GET", "/worlds")
            except HTTPException as exc:
                return f"The Minecraft server can't answer right now: {exc.detail}"
            slots = [r for r in got_worlds.get("slots", []) if isinstance(r, dict)]
            want = f"slot{slot_n}" if slot_n is not None else None
            rec = next(
                (r for r in slots if (r.get("id") == want if want else r.get("active"))), None
            )
            if rec is None or not rec.get("exists", True):
                return (
                    "No world is loaded. Say which slot (1-5) to look in."
                    if want is None
                    else f"Slot {slot_n} is empty — there's no world there."
                )
            world = str(rec.get("folder") or "")
        if not world:
            return "The Minecraft server can't say which world is loaded."
        raw_player = str(arguments.get("player") or "").strip()
        async with scoped_session(maker, ctx.session) as s:
            got = await markers.read(s, world)
            if asker is not None:
                who: str | None = asker.gamertag
                owner: Player | None = Player(asker.key, asker.gamertag)
            elif raw_player:
                tag = check_gamertag(raw_player)
                if tag is None:
                    return "player must be a gamertag."
                who = tag
                owner = None
            else:
                who = None
                owner = await chat_player_for(s, settings_store, ctx.session, ctx.agent_session_id)
            places = await _places(s, owner.key, world) if owner is not None else []
        body, brief = listing(
            got,
            who=who,
            asker=asker,
            show_beacons=show_beacons,
            show_gates=show_gates,
            places=places,
            place_owner=owner.gamertag if owner is not None else None,
        )
        tail = (
            "These are the asker's own. Say where in words, with the coordinates."
            if asker is not None
            else "Positions are the pack's own records; the full map draws them too."
        )
        return ToolOutput(f"{fence(body)}\n{tail}", result_brief=brief)

    return {"mc_gates_beacons": _guarded("mc_gates_beacons", mc_gates_beacons)}
