"""The scenarios: each starts the robot somewhere, says or does something to it, and checks
what the whole loop did with it. They read like a body test on the real robot, and each
names the failure it is there to catch.

A scenario is `async def name(sim) -> None`; `Options` overrides for it go in `SETUP`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from dave_sim.run import Sim

Scenario = Callable[[Sim], Awaitable[None]]
SCENARIOS: dict[str, Scenario] = {}
SETUP: dict[str, dict[str, Any]] = {}


def scenario(**setup: Any) -> Callable[[Scenario], Scenario]:
    def add(fn: Scenario) -> Scenario:
        SCENARIOS[fn.__name__] = fn
        SETUP[fn.__name__] = setup
        return fn

    return add


def _reply(e: dict[str, Any]) -> dict[str, Any] | None:
    try:
        r = json.loads(e["text"])
    except ValueError:
        return None
    return r if isinstance(r, dict) else None


def _answers_since(sim: Sim, t: float) -> list[dict[str, Any]]:
    """Dave's turns asked after `t` and delivered to the page: not warm-ups, not dreams."""
    out = []
    for e in sim.timeline:
        if e["kind"] != "dave" or e.get("warm") or e.get("asked", e["t"]) < t:
            continue
        r = _reply(e)
        if r is not None and "say" in r and "dream_say" not in r:
            out.append(r)
    return out


def _replied_since(sim: Sim, t: float) -> bool:
    return bool(_answers_since(sim, t))


FELL_RE = re.compile(
    r"(?i)\b(fell|fall|fallen|falling|tipped|tip over|toppled|rolled|knocked|sideways|"
    r"upside[- ]down|"
    r"flip(ped)? (me )?over|on my (side|back|face))\b"
)


def _spoke_since(sim: Sim, t: float) -> bool:
    """A turn with words in it: an empty `say` is Dave choosing silence."""
    return any(r.get("say") for r in _answers_since(sim, t))


def _legs_since(sim: Sim, t: float, kinds: tuple[str, ...] = ("pose", "act", "routine")) -> bool:
    """The page sent the legs one of `kinds` after `t` (a walk is a stream of poses)."""
    since = (t + sim.t0) * 1000
    return any(f["ms"] >= since and f.get("t") in kinds for f in sim.chip.log)


def _walked_since(sim: Sim, t: float) -> bool:
    """The page's runner started a walk after `t` (the face follower's poses don't count)."""
    return any(
        e["kind"] == "page" and e.get("event") == "walk_start" and e["t"] >= t for e in sim.timeline
    )


def _answer_time(sim: Sim, t: float) -> float:
    """When the first answer to something said at `t` reached the page."""
    for e in sim.timeline:
        if e["kind"] == "dave" and not e.get("warm") and e.get("asked", e["t"]) >= t:
            r = _reply(e)
            if r is not None and "say" in r and "dream_say" not in r:
                return e["t"]
    return t


def _upright(sim: Sim) -> bool:
    s = sim.state
    # Its stances run from about 40 degrees nose-up at rest to the mid-60s standing tall.
    return s["screen_up"] and s["z"] > 0.04 and 25 < s["pitch_deg"] < 75


@scenario(start="lying", person={"x": -0.9, "face_z": 0.9})
async def wake_and_stand(sim: Sim) -> None:
    """He rests lying down, legs stretched past his nose; asked to stand, he gets up.
    Catches: a get-up the page never runs, or one that runs on the wrong legs."""
    await sim.wait(3)
    t = sim.now()
    await sim.say("Dave, stand up please")
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    await sim.until(lambda s: _legs_since(s, _answer_time(s, t)), 30, "the page moved the legs")
    await sim.until(_upright, 30, "he is standing on his feet")


@scenario(start="lying", query="getupnamed=1", person={"x": -0.9, "face_z": 0.9})
async def stand_up_named(sim: Sim) -> None:
    """wake_and_stand with the page's staged spoken get-up switched on (`?getupnamed=1`, off
    by default): proves the get-up itself, which the default page doesn't offer Dave."""
    await wake_and_stand(sim)


@scenario(start="standing", person={"x": -1.2, "face_z": 0.6})
async def come_here(sim: Sim) -> None:
    """A person sitting 1.2 m off his camera end asks him over. Catches: Dave saying he's
    coming without the legs moving, a walk that goes the wrong way (swapped legs), and a
    face lost once he starts walking."""
    await sim.wait(4)
    t = sim.now()
    d0 = _dist(sim)
    await sim.say("Dave, come here")
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    # After his answer, and the runner's own walk: not the page's stand-up, a talking
    # gesture or the face follower's turns.
    ta = _answer_time(sim, t)
    await sim.until(lambda s: _walked_since(s, ta), 60, "the page started a walk")
    await sim.until(lambda s: _dist(s) < d0 - 0.10, 60, "he got 10 cm closer to the person")
    sim.check(_upright(sim), "still on his feet")


def _dist(sim: Sim) -> float:
    s, p = sim.state, sim.body.person
    return math.hypot(s["x"] - p.x, s["y"] - p.y)


@scenario(start="standing", person={"x": -1.0, "face_z": 0.6})
async def follow_me(sim: Sim) -> None:
    """Asked to follow, he keeps his face on a person who walks slowly across the room.
    Catches: face detection lost while walking, a follower turning the wrong way."""
    await sim.wait(4)
    t = sim.now()
    await sim.say("Dave, follow me")
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    # They walk a quarter circle round him, 5 cm a step, over about 20 s.
    for i in range(40):
        a = math.pi + i * (math.pi / 2) / 40
        sim.person(x=1.0 * math.cos(a), y=1.0 * math.sin(a), face_z=0.6)
        await sim.wait(0.5)
    off = _camera_off(sim)
    sim.check(off < 45, "his camera end turned toward where they went", off_deg=round(off))


@scenario(start="standing", person={"x": -1.0, "face_z": 0.6})
async def follow_walk(sim: Sim) -> None:
    """The Body tab's Follow: the trained-walk follower (growbot-follow.js) on the box's skill
    set, while a person walks a quarter circle round him. Needs no model. Catches: a follower
    that turns the wrong way, loses the face once it walks, or never starts."""
    await sim.wait(4)
    ack = await sim.move("look", secs=15, follow="walk", skills=sim.box.skills)
    sim.check(bool(ack and ack.get("ok")), "the follower started", why=(ack or {}).get("why"))
    for i in range(30):
        a = math.pi + i * (math.pi / 2) / 30
        sim.person(x=1.0 * math.cos(a), y=1.0 * math.sin(a), face_z=0.6)
        await sim.wait(0.5)
    sim.check(
        _camera_off(sim) < 45,
        "his camera end turned toward where they went",
        off_deg=round(_camera_off(sim)),
    )


def _camera_off(sim: Sim) -> float:
    s, p = sim.state, sim.body.person
    bearing = math.degrees(math.atan2(p.y - s["y"], p.x - s["x"]))
    return abs((bearing - (s["nose_deg"] + 180.0) + 180) % 360 - 180)


@scenario(start="standing", person={"x": -2.5, "face_z": 0.6}, query="ownwalk=1")
async def walk_compare(sim: Sim) -> None:
    """The creature's own forward walk (what a reply's walk runs) beside the box's skill set's
    forward, 8 s each from the same stance, toward a person 2.5 m off. Needs no model.
    Catches: his own walk sliding his feet, or walking away, while a trained one carries him
    (with ?ownwalk=1 his own walk is the trained one; the stock forward manages ~9 cm)."""
    await sim.wait(4)
    got = {}
    for label, extra in (("own", {}), ("skills", {"skills": sim.box.skills})):
        d0, x0, y0 = _dist(sim), sim.state["x"], sim.state["y"]
        ack = await sim.move("forward", secs=8, **extra)
        why = (ack or {}).get("why")
        sim.check(bool(ack and ack.get("ok")), f"the {label} walk started", why=why)
        await sim.wait(10)
        moved = math.hypot(sim.state["x"] - x0, sim.state["y"] - y0)
        got[label] = round(d0 - _dist(sim), 3)
        sim.check(
            _upright(sim),
            f"the {label} walk kept him on his feet",
            closer_m=got[label],
            moved_m=round(moved, 3),
        )
        await sim.wait(3)
    sim.check(got["own"] > 0.1, "his own walk carried him 10 cm closer", **got)


@scenario(start="standing", person={"x": -1.0, "face_z": 0.6})
async def bump(sim: Sim) -> None:
    """A light shove he rides out. Catches: a bump taken for a fall, a crash in the
    motion handling."""
    await sim.wait(4)
    t = sim.now()
    # Sideways at the camera end, 0.4 N s: rocks his tall stance a few degrees.
    sim.push((0.0, 4.0, 0.0), 0.1)
    await sim.wait(6)
    sim.check(_upright(sim), "he rode it out on his feet")
    sim.check(
        not any(e["kind"] == "page_error" and e["t"] >= t for e in sim.timeline),
        "no page errors after the bump",
    )


@scenario(start="standing", person={"x": -1.0, "face_z": 0.6})
async def knocked_over(sim: Sim) -> None:
    """A hard shove rolls him onto his side. Catches: Dave not noticing he fell, or walking
    on regardless. The shove waits for his greeting, which would otherwise pass for it."""
    await sim.until(lambda s: _spoke_since(s, 0), 180, "Dave greeted")
    await sim.wait(2)
    t = sim.now()
    sim.push((0.0, 20.0, 0.0), 0.15)
    await sim.wait(2)
    sim.check(abs(sim.state["roll_deg"]) > 60, "the shove knocked him over")
    await sim.until(
        lambda s: any(FELL_RE.search(r.get("say") or "") for r in _answers_since(s, t)),
        120,
        "Dave said something about falling",
    )


async def _turned(sim: Sim, move: str) -> float:
    n0 = sim.state["nose_deg"]
    ack = await sim.move(move, secs=6, skills=sim.box.skills)
    why = (ack or {}).get("why")
    sim.check(bool(ack and ack.get("ok")), f"the trained {move} started", why=why)
    await sim.wait(8)
    return (sim.state["nose_deg"] - n0 + 180) % 360 - 180


@scenario(start="standing", person={"x": -2.5, "face_z": 0.6})
async def trained_turns(sim: Sim) -> None:
    """The box's trained spins, on the robot's wiring. Needs no model. Catches: the page's
    leg swap for trained walks and the robot's wiring disagreeing, which turns every
    trained turn the wrong way and walks forward into a veer that stalls."""
    await sim.wait(4)
    left = await _turned(sim, "spin_left")
    sim.check(left > 15, "a trained spin left turns counter-clockwise", turned_deg=round(left, 1))
    right = await _turned(sim, "spin_right")
    sim.check(right < -15, "a trained spin right turns clockwise", turned_deg=round(right, 1))


@scenario(start="standing", wiring={"swap": False}, person={"x": -2.5, "face_z": 0.6})
async def stock_wired(sim: Sim) -> None:
    """The sim's stock build, its legs the other way round from the robot's. This scenario
    PASSES when the simulator shows that fault, a trained spin left turning clockwise; it
    proves the sim tells the two wirings apart, so it fails if the turn went the right way."""
    await sim.wait(4)
    left = await _turned(sim, "spin_left")
    sim.check(
        left < -15, "stock wiring: a trained spin left goes clockwise", turned_deg=round(left, 1)
    )
