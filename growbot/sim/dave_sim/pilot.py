"""A stand-in brain that answers the way a sensible Dave should, reading the sim's ground truth
instead of a camera: told to find something in the house, it sets the goal, turns toward it,
walks, keeps sending walk on each look and says it found it once there; left alone, it picks
a piece of furniture it hasn't been near and goes to look.

It tests the page's plumbing (goals that stick, walks that carry on, looks while walking, the
no-look stop) with no box: the real model then only has to prove the thinking. `latency`
stands in for the box's slower turns.

    python -m dave_sim find_piano --pilot

It also keeps Dave's map (growbot-map.js) the way a careful Dave would: it names each room
it enters, puts each piece of furniture and the doorway it sees on the map (where it sits
across the picture and how far, from view.py), and says `at` when it reaches one. Asked to
"use your map" to get somewhere, it steers by the map alone: `where`, then the turn and the
distance the answer gives, then `where` again, so the map's own directions are what is
tested.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import TYPE_CHECKING, Any

from dave_sim import view

if TYPE_CHECKING:
    from dave_sim.run import Sim

ARRIVED_M = 0.5
FACING_DEG = 25.0
# The page's stock spin turns roughly this fast; the pilot asks for short spins and looks
# again rather than trusting it.
SPIN_DEG_S = 30.0
EXPLORE_RE = re.compile(r"QUIET — nothing has happened for a while")
MAP_NAV_RE = re.compile(r"\buse your map to (?:go to|get to|find) (?:the )?([a-z ]+?)[.?!]")
WHERE_RE = re.compile(r'you looked at your map for "([^"]*)": ([^\n]*)')
TURN_RE = re.compile(r"walk:\{turn:(-?\d+)\}")
DIST_RE = re.compile(r"about ([\d.]+) m")
# A thing goes on the map again only after this long, as a Dave who glanced back would.
REMARK_S = 40.0
# The map's guess of his pace (growbot-map.js PACE.forward), so a walk covers what it says.
PACE_M_S = 0.04


def _through(d: Any, x: float, y: float) -> tuple[float, float]:
    """Where to head for door `d` from (x, y): in front of it on this side, or once lined up
    in front of it, through to the far side."""
    n = 0 if d.along == "y" else 1  # the wall's normal axis: x for a wall running along y
    pos, mid = (x, y), (d.x, d.y)
    side = 1.0 if pos[n] > mid[n] else -1.0
    lined = abs(pos[1 - n] - mid[1 - n]) <= 0.25 and abs(pos[n] - mid[n]) <= 0.7
    off = -0.6 * side if lined else 0.5 * side
    return (d.x + off, d.y) if n == 0 else (d.x, d.y + off)


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
        self.room = ""
        self.marked: dict[str, float] = {}
        self.nav = ""
        self.nav_walk = 0.0
        self.turn_until = 0.0
        self.exploring = False
        # An errand ("find the cat and come back and tell me"): where he was asked, what he
        # has to report once back there, and the asks already done (the ask stays in the
        # prompt's tail for a while and must not start him off again).
        self.home: tuple[float, float] | None = None
        self.report = ""
        self.done: set[str] = set()

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
        house = getattr(sim.body, "layout", None) if sim is not None else None
        if house is None:
            return {"say": ""}
        ops = self._map_ops(house)
        out = self._answer(house, text)
        if ops:
            out["map"] = ops + ([out["map"]] if isinstance(out.get("map"), dict) else [])
        return out

    def _map_ops(self, house: Any) -> list[dict[str, Any]]:
        """What a careful Dave adds to his map on this look: the room when he has walked
        into a new one, and each thing in view not marked for a while."""
        assert self.sim is not None
        s = self.sim.state
        now = self.sim.now()
        ops: list[dict[str, Any]] = []
        room = house.room_at(s["x"], s["y"])
        if room and room != self.room:
            ops.append({"room": room})
            self.room = room
        for v in view.visible(self.sim):
            if len(ops) >= 4:
                break
            if now - self.marked.get(v["name"], -1e9) < REMARK_S:
                continue
            self.marked[v["name"]] = now
            if v["name"] == "door":
                ops.append({"door": v["to"], "x": v["x"], "m": round(v["m"], 1)})
            else:
                ops.append({"saw": v["name"], "x": v["x"], "m": round(v["m"], 1)})
        return ops

    def _answer(self, house: Any, text: str) -> dict[str, Any]:
        tail = text[-600:].lower()
        got = WHERE_RE.search(text)
        if got and not self.nav:
            # Asked where something is: say what the map said.
            room = re.search(r"is in the ([a-z ]+?)[,.]", got.group(2))
            what = got.group(1)
            if room:
                return {"say": f"The {what} is in the {room.group(1)}."}
            return {"say": f"My map doesn't have the {what}."}
        ask = re.search(r"where is (?:the |my )?([a-z ]+?)\?", tail)
        if ask and not self.nav:
            return {"where": ask.group(1), "say": "Let me check my map."}
        nav = MAP_NAV_RE.search(tail)
        if nav and nav.group(1).strip() in house.ITEM:
            self.nav, self.target = nav.group(1).strip(), ""
            return {
                "where": self.nav,
                "say": "Let me check my map.",
                "goal": f"go to the {self.nav}",
            }
        if self.nav:
            return self._by_map(house, text)
        if self.report:
            return self._home(house)
        for it in house.ITEMS:
            asked = rf"\b(find|over to|go to) (the |a |my )?{re.escape(it.name)}\b"
            if it.name != "rug" and it.name not in self.done and re.search(asked, tail):
                self.target = it.name
                if re.search(r"\bcome back\b", tail) and self.home is None:
                    s = self.sim.state if self.sim is not None else {"x": 0.0, "y": 0.0}
                    self.home = (s["x"], s["y"])
        if re.search(r"\bexplore\b", tail):
            self.exploring = True
        if not self.target and (self.exploring or EXPLORE_RE.search(text)):
            self._next(house)
        if not self.target:
            return {"say": ""}
        return self._toward(house)

    def _next(self, house: Any) -> None:
        """The nearest piece of furniture not visited yet, to go and look at."""
        assert self.sim is not None
        s = self.sim.state
        left = [
            it
            for it in house.ITEMS
            if it.name not in self.seen and it.name not in ("rug", "piano bench")
        ]
        if left:
            near = min(left, key=lambda it: house.distance_to(it.name, s["x"], s["y"]))
            self.target = near.name

    def _toward(self, house: Any) -> dict[str, Any]:
        assert self.sim is not None
        s = self.sim.state
        name = self.target
        goal = {"goal": f"find the {name}", "phase": "acting"}
        if house.distance_to(name, s["x"], s["y"]) < ARRIVED_M:
            self.seen.add(name)
            self.target = ""
            if self.exploring:
                # On to the next, without finishing the goal: an explorer keeps going.
                self._next(house)
                if self.target:
                    nxt = self._toward(house)
                    nxt["say"] = f"Found the {name}. Now the {self.target}."
                    nxt["map"] = {"at": name}
                    return nxt
            if self.home is not None:
                # Found it on an errand: now back to whoever sent him, to tell them.
                self.done.add(name)
                room = house.room_at(house.ITEM[name].x, house.ITEM[name].y)
                self.report = f"The {name} is in the {room}."
                out = self._home(house)
                out["say"] = f"There's the {name}. Going back to tell you."
                out["map"] = {"at": name}
                return out
            return {
                "say": f"I found the {name}!",
                "found": True,
                "phase": "finished",
                "map": {"at": name},
            }
        it = house.ITEM[name]
        tx, ty = it.x, it.y
        return self._steer(house, tx, ty, name, goal)

    def _home(self, house: Any) -> dict[str, Any]:
        """Back to where he was sent from; there, say what he found."""
        assert self.sim is not None and self.home is not None
        s = self.sim.state
        hx, hy = self.home
        if math.hypot(hx - s["x"], hy - s["y"]) < 0.5:
            say, self.report, self.home = self.report, "", None
            return {"say": say, "found": True, "phase": "finished"}
        goal = {"goal": "go back and tell them", "phase": "acting"}
        return self._steer(house, hx, hy, "you", goal)

    def _steer(
        self, house: Any, tx: float, ty: float, name: str, goal: dict[str, Any]
    ) -> dict[str, Any]:
        """Turn toward (tx, ty), or walk once facing it; through doorways on the way."""
        assert self.sim is not None
        the = "" if name == "you" else "the "
        s = self.sim.state
        here, there = house.room_at(s["x"], s["y"]), house.room_at(tx, ty)
        if here and there and here != there and house.route(here, there):
            # Through the next doorway on the way, not into the wall beside it: first to a
            # spot in front of it on this side, then straight through.
            tx, ty = _through(house.route(here, there)[0], s["x"], s["y"])
        facing = s["nose_deg"] + 180.0  # the camera looks out of the tail
        bearing = math.degrees(math.atan2(ty - s["y"], tx - s["x"]))
        err = _wrap(bearing - facing)
        if abs(err) > FACING_DEG:
            secs = round(max(1.0, min(4.0, abs(err) / SPIN_DEG_S)), 1)
            left = err > 0  # counter-clockwise from above
            return {
                "say": f"Turning toward {the}{name}.",
                "walk": {
                    "gait": "spin_left" if left else "spin_right",
                    "dir": "left" if left else "right",
                    "secs": secs,
                },
                **goal,
            }
        return {"say": f"Walking to {the}{name}.", "walk": {"dir": "fwd", "secs": 8}, **goal}

    def _by_map(self, house: Any, text: str) -> dict[str, Any]:
        """Steering by the map's answers alone: turn the way it says, walk the distance it
        says, ask again. The ground truth only says when he has arrived."""
        assert self.sim is not None
        s = self.sim.state
        name = self.nav
        goal = {"goal": f"go to the {name}", "phase": "acting"}
        if house.distance_to(name, s["x"], s["y"]) < ARRIVED_M:
            self.nav = ""
            return {
                "say": f"I made it to the {name}!",
                "found": True,
                "phase": "finished",
                "map": {"at": name},
            }
        got = WHERE_RE.search(text)
        if got:
            ans = got.group(2)
            turn = TURN_RE.search(ans)
            dist = DIST_RE.search(ans)
            self.nav_walk = float(dist.group(1)) if dist else 0.5
            if turn and abs(int(turn.group(1))) >= 12:
                deg = int(turn.group(1))
                # The gyro stops the spin at the angle; the looks while it turns wait for it.
                self.turn_until = self.sim.now() + abs(deg) / 25.0 * 1.6 + 2.0
                return {"say": "This way.", "walk": {"turn": deg}, **goal}
        if self.sim.now() < self.turn_until:
            return {"say": "", **goal}
        if self.nav_walk > 0:
            secs = round(max(3.0, min(15.0, self.nav_walk / PACE_M_S)), 1)
            self.nav_walk = 0.0
            return {"say": f"Heading for the {name}.", "walk": {"dir": "fwd", "secs": secs}, **goal}
        return {"where": name, "say": "Let me check my map.", **goal}
