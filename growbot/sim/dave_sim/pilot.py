"""A stand-in brain that answers the way a sensible Dave should, reading the sim's ground truth
instead of a camera: told to find something in the house, it sets the goal, turns toward it,
walks, keeps sending walk on each look and says it found it once there; left alone, it picks
a piece of furniture it hasn't been near and goes to look.

It tests the page's plumbing (goals that stick, walks that carry on, looks while walking, the
no-look stop) with no box: the real model then only has to prove the thinking. `latency`
stands in for the box's slower turns.

    python -m dave_sim find_piano --pilot
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dave_sim.run import Sim

ARRIVED_M = 0.5
FACING_DEG = 25.0
# The page's stock spin turns roughly this fast; the pilot asks for short spins and looks
# again rather than trusting it.
SPIN_DEG_S = 30.0
EXPLORE_RE = re.compile(r"QUIET — nothing has happened for a while")


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


def _turn_text(req: dict[str, Any]) -> str:
    for m in reversed(req.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            return str(c)
    return ""


class Pilot:
    def __init__(self, latency: float = 4.0) -> None:
        self.latency = latency
        self.sim: Sim | None = None
        self.target = ""
        self.seen: set[str] = set()
        self.log: list[dict[str, Any]] = []

    def bind(self, sim: Sim) -> None:
        self.sim = sim

    async def __call__(self, req: dict[str, Any]) -> str:
        if int(req.get("max_tokens") or 9) <= 2:
            return "{}"  # a warm-up: the page only wants the prompt read
        await asyncio.sleep(self.latency)
        out = self.answer(_turn_text(req))
        self.log.append(out)
        return json.dumps(out)

    def answer(self, text: str) -> dict[str, Any]:
        sim = self.sim
        house = sim.sb.house if sim is not None and sim.o.scene == "house" else None
        if house is None:
            return {"say": ""}
        tail = text[-600:].lower()
        for it in house.ITEMS:
            asked = rf"\b(find|over to|go to) (the |a |my )?{re.escape(it.name)}\b"
            if it.name != "rug" and re.search(asked, tail):
                self.target = it.name
        if not self.target and EXPLORE_RE.search(text):
            s = sim.state
            left = [
                it
                for it in house.ITEMS
                if it.name not in self.seen and it.name not in ("rug", "piano bench")
            ]
            if left:
                near = min(left, key=lambda it: house.distance_to(it.name, s["x"], s["y"]))
                self.target = near.name
        if not self.target:
            return {"say": ""}
        return self._toward(house)

    def _toward(self, house: Any) -> dict[str, Any]:
        assert self.sim is not None
        s = self.sim.state
        name = self.target
        goal = {"goal": f"find the {name}", "phase": "acting"}
        if house.distance_to(name, s["x"], s["y"]) < ARRIVED_M:
            self.seen.add(name)
            self.target = ""
            return {"say": f"I found the {name}!", "found": True, "phase": "finished"}
        it = house.ITEM[name]
        # In another room: make for the doorway first (a point just on this side of it, then
        # one just through), not straight at the wall between.
        here, there = house.room_at(s["x"], s["y"]), it.room
        door_y = sum(house.DOOR) / 2.0
        if here and here != there:
            side = 1.0 if s["x"] > house.DIVIDE_X else -1.0
            near_door = abs(s["y"] - door_y) < 0.2 and abs(s["x"] - house.DIVIDE_X) < 0.45
            tx = house.DIVIDE_X - side * 0.4 if near_door else house.DIVIDE_X + side * 0.35
            it = type("Point", (), {"x": tx, "y": door_y})()
        facing = s["nose_deg"] + 180.0  # the camera looks out of the tail
        bearing = math.degrees(math.atan2(it.y - s["y"], it.x - s["x"]))
        err = _wrap(bearing - facing)
        if abs(err) > FACING_DEG:
            secs = round(max(1.0, min(4.0, abs(err) / SPIN_DEG_S)), 1)
            left = err > 0  # counter-clockwise from above
            return {
                "say": f"Turning toward the {name}.",
                "walk": {
                    "gait": "spin_left" if left else "spin_right",
                    "dir": "left" if left else "right",
                    "secs": secs,
                },
                **goal,
            }
        return {"say": f"Walking to the {name}.", "walk": {"dir": "fwd", "secs": 8}, **goal}
