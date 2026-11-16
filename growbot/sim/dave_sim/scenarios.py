"""The scenarios: each starts the robot somewhere, says or does something to it, and checks
what the whole loop did with it. They read like a body test on the real robot, and each
names the failure it is there to catch.

A scenario is `async def name(sim) -> None`; `Options` overrides for it go in `SETUP`.
"""

from __future__ import annotations

import json
import math
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


def _replied_since(sim: Sim, t: float) -> bool:
    return any(e["kind"] == "dave" and e["t"] >= t for e in sim.timeline)


def _spoke_since(sim: Sim, t: float) -> bool:
    """A reply with words in it: an empty `say` is Dave choosing silence."""
    for e in sim.timeline:
        if e["kind"] == "dave" and e["t"] >= t:
            try:
                said = json.loads(e["text"]).get("say")
            except (ValueError, AttributeError):
                said = None
            if said:
                return True
    return False


def _legs_since(sim: Sim, t: float) -> bool:
    """The page sent the legs anything (a walk's poses, a gesture) after `t`."""
    since = (t + sim.t0) * 1000
    return any(f["ms"] >= since and f.get("t") in ("pose", "act", "routine") for f in sim.chip.log)


def _face_seen(sim: Sim, t: float) -> bool:
    return any(
        e["kind"] == "page" and e["t"] >= t and e.get("event") in ("face", "faces", "face_seen")
        for e in sim.timeline
    )


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
    await sim.until(lambda s: _replied_since(s, t), 90, "Dave answered")
    await sim.until(lambda s: _legs_since(s, t), 30, "the page moved the legs")
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
    await sim.until(lambda s: _replied_since(s, t), 90, "Dave answered")
    await sim.until(lambda s: _legs_since(s, t), 20, "the page moved the legs")
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
    await sim.until(lambda s: _replied_since(s, t), 90, "Dave answered")
    # They walk a quarter circle round him, 5 cm a step, over about 20 s.
    for i in range(40):
        a = math.pi + i * (math.pi / 2) / 40
        sim.person(x=1.0 * math.cos(a), y=1.0 * math.sin(a), face_z=0.6)
        await sim.wait(0.5)
    s = sim.state
    p = sim.body.person
    bearing = math.degrees(math.atan2(p.y - s["y"], p.x - s["x"]))
    camera_end = s["nose_deg"] + 180.0
    off = abs((bearing - camera_end + 180) % 360 - 180)
    sim.check(off < 45, "his camera end turned toward where they went", off_deg=round(off))


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
    on regardless."""
    await sim.wait(4)
    t = sim.now()
    sim.push((0.0, 20.0, 0.0), 0.15)
    await sim.wait(2)
    sim.check(abs(sim.state["roll_deg"]) > 60, "the shove knocked him over")
    await sim.until(lambda s: _spoke_since(s, t), 60, "Dave said something about falling")


@scenario(start="standing", wiring={"swap": True}, person={"x": -1.2, "face_z": 0.6})
async def swapped_legs(sim: Sim) -> None:
    """Last night's robot: leg pins crossed, calibration not told. Asked over, he should
    not end up farther away while saying he's coming. This one is expected to show the
    fault; it proves the simulator catches it."""
    await come_here(sim)
