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

from dave_sim import plan
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
    walks = _page_events_since(sim, ta, "walk_start")
    sim.check(
        any(w.get("gait") == "trained" for w in walks),
        "on his trained walk",
        gaits=[w.get("gait") for w in walks],
    )
    sim.check(_upright(sim), "still on his feet")


def _page_event_since(sim: Sim, t: float, name: str) -> bool:
    return any(e["kind"] == "page" and e.get("event") == name and e["t"] >= t for e in sim.timeline)


def _page_events_since(sim: Sim, t: float, name: str) -> list[dict[str, Any]]:
    return [
        e.get("data") or {}
        for e in sim.timeline
        if e["kind"] == "page" and e.get("event") == name and e["t"] >= t
    ]


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
        sim.person(x=1.2 * math.cos(a), y=1.2 * math.sin(a), face_z=1.55)
        await sim.wait(0.5)
    off = _camera_off(sim)
    sim.check(off < 45, "his camera end turned toward where they went", off_deg=round(off))


@scenario(start="standing", person={"x": -1.2, "face_z": 1.55})
async def follow_walk(sim: Sim) -> None:
    """The Body tab's Follow: the trained-walk follower (growbot-follow.js) on the box's skill
    set, while a standing person walks a quarter circle round him. Needs no model. Catches: a
    follower that turns the wrong way, loses the face once it walks, or never starts. A seated
    face (0.6 m) at 1 m tilts the camera down, where the trained look barely turns: it kept up
    2 runs in 5 (2026-11-17), the limit the follower's pitchTurnMax note records."""
    await sim.wait(4)
    ack = await sim.move("look", secs=15, follow="walk", skills=sim.box.skills)
    sim.check(bool(ack and ack.get("ok")), "the follower started", why=(ack or {}).get("why"))
    for i in range(30):
        a = math.pi + i * (math.pi / 2) / 30
        sim.person(x=1.2 * math.cos(a), y=1.2 * math.sin(a), face_z=1.55)
        await sim.wait(0.5)
    sim.check(
        _camera_off(sim) < 45,
        "his camera end turned toward where they went",
        off_deg=round(_camera_off(sim)),
    )


@scenario(start="standing", person={"x": -1.2, "face_z": 1.55})
async def follow_walks_after(sim: Sim) -> None:
    """The trained-walk follower ("follow me") after a standing person who walks slowly away
    for 40 s and then sideways for 20. Needs no model. Catches: a follower that only turns,
    turns away from the face (the camera flip), or loses them once they are out of reach."""
    await sim.wait(4)
    ack = await sim.move("look", secs=15, follow="walk", skills=sim.box.skills)
    sim.check(bool(ack and ack.get("ok")), "the follower started", why=(ack or {}).get("why"))
    x0, y0, px, py, gaps = sim.state["x"], sim.state["y"], -1.2, 0.0, []
    for i in range(120):
        px, py = (px - 0.015, py) if i < 80 else (px, py + 0.015)
        sim.person(x=px, y=py, face_z=1.55)
        await sim.wait(0.5)
        if i % 10 == 9:
            gaps.append(round(_dist(sim), 2))
    walked = math.hypot(sim.state["x"] - x0, sim.state["y"] - y0)
    sim.check(walked > 0.5, "he walked after them", walked_m=round(walked, 2), gaps_m=gaps)
    # Standing still would leave them about 2.5 m off.
    sim.check(gaps[-1] < 2.0, "and kept within reach", gap_m=gaps[-1])
    off = _camera_off(sim)
    sim.check(off < 30, "his camera end still on them", off_deg=round(off))


@scenario(start="standing", person={"x": -1.2, "face_z": 1.55}, cam_flip=None)
async def follow_unflipped(sim: Sim) -> None:
    """The follower as a phone runs it before its owner sets the camera flip. This scenario
    PASSES when the simulator shows the fault it found, a follow turning away from a person
    who backs off; it fails once a fix to the sim's camera or wiring makes the unflipped
    follower work, which says the flip step is no longer needed."""
    await sim.wait(4)
    ack = await sim.move("look", secs=15, follow="walk", skills=sim.box.skills)
    sim.check(bool(ack and ack.get("ok")), "the follower started", why=(ack or {}).get("why"))
    px = -1.2
    for _ in range(80):
        px -= 0.015
        sim.person(x=px, y=0.0, face_z=1.55)
        await sim.wait(0.5)
    off, gap = _camera_off(sim), _dist(sim)
    sim.check(
        off > 30 or gap > 2.0,
        "unflipped: he turned off them or fell behind",
        off_deg=round(off),
        gap_m=round(gap, 2),
    )


@scenario(start="standing", person={"x": -1.2, "face_z": 1.55})
async def follow_me_away(sim: Sim) -> None:
    """Asked to follow, by voice, a standing person who then walks slowly away. Catches: a
    reply without the follow verb, a follow verb that starts nothing, and a follower that
    only turns."""
    await sim.wait(4)
    t = sim.now()
    await sim.say("Dave, follow me")
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    # The follower itself, not a plain walk answering "follow me", which would also close in.
    await sim.until(lambda s: _page_event_since(s, t, "follow_lock"), 20, "the follower locked on")
    x0, y0, px = sim.state["x"], sim.state["y"], -1.2
    for _ in range(60):
        px -= 0.015
        sim.person(x=px, y=0.0, face_z=1.55)
        await sim.wait(0.5)
    walked = math.hypot(sim.state["x"] - x0, sim.state["y"] - y0)
    sim.check(walked > 0.3, "he walked after them", walked_m=round(walked, 2))
    sim.check(_dist(sim) < 2.0, "and kept within reach", gap_m=round(_dist(sim), 2))


@scenario(start="standing", person={"x": -2.5, "face_z": 1.55})
async def stop_stop(sim: Sim) -> None:
    """A long walk, then "stop stop" said mid-walk. Needs no model: the page halts on the
    words before any turn. The walk is the pad's skill-set one, on the same runner as his own
    walks and the follower. Catches: a trained walk that runs on."""
    await sim.wait(4)
    x0, y0, t = sim.state["x"], sim.state["y"], sim.now()
    ack = await sim.move("forward", secs=15, skills=sim.box.skills)
    sim.check(bool(ack and ack.get("ok")), "the walk started", why=(ack or {}).get("why"))
    # A short duty grant would end the walk on its own about when the words land.
    await sim.until(lambda s: _page_event_since(s, t, "walk_start"), 10, "the runner began")
    starts = _page_events_since(sim, t, "walk_start")
    granted = max((w.get("granted") or 0 for w in starts), default=0)
    sim.check(granted >= 10, "granted long enough to outlast the words", granted_s=granted)
    await sim.wait(5)
    went = math.hypot(sim.state["x"] - x0, sim.state["y"] - y0)
    sim.check(went > 0.05, "he was walking", moved_m=round(went, 3))
    await sim.say("stop stop")
    # Measured before the page's own stance a few seconds later (rest_tall), which shuffles
    # the feet by a couple of centimetres.
    await sim.wait(0.5)
    x1, y1 = sim.state["x"], sim.state["y"]
    await sim.wait(2)
    drift = math.hypot(sim.state["x"] - x1, sim.state["y"] - y1)
    sim.check(drift < 0.02, "still from half a second after", moved_after_m=round(drift, 3))
    sim.check(_upright(sim), "still on his feet")


def _camera_off(sim: Sim) -> float:
    s, p = sim.state, sim.body.person
    bearing = math.degrees(math.atan2(p.y - s["y"], p.x - s["x"]))
    return abs((bearing - (s["nose_deg"] + 180.0) + 180) % 360 - 180)


@scenario(start="standing", person={"x": -2.5, "face_z": 0.6})
async def walk_compare(sim: Sim) -> None:
    """The pad's built-in (stock) forward beside the box's skill set's forward, 8 s each from
    the same stance, toward a person 2.5 m off. Needs no model. Catches: the trained walk
    sliding his feet or walking away (it carries him; the stock one manages ~9 cm). Dave's
    own reply walks take the trained one: come_here holds that."""
    await sim.wait(4)
    got = {}
    for label, extra in (("stock", {}), ("skills", {"skills": sim.box.skills})):
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
    sim.check(got["skills"] > 0.1, "the trained walk carried him 10 cm closer", **got)


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


# ---------------------------------------------------------------------- the house


def _near(sim: Sim, item: str) -> float:
    s = sim.state
    return float(sim.body.layout.distance_to(item, s["x"], s["y"]))


def _found_said(sim: Sim, t: float, word: str) -> bool:
    """A turn since `t` that says he found it, or names it with found:true."""
    for r in _answers_since(sim, t):
        say = str(r.get("say") or "").lower()
        if r.get("found") in (True, "true") and word in (say + json.dumps(r.get("goal", ""))):
            return True
        if word in say and re.search(r"(?i)\b(found|here it is|there it is|i see)\b", say):
            return True
    return False


def _pose_at(sim: Sim, t: float) -> dict[str, Any]:
    """Where he was at sim time `t`: the nearest recorded pose."""
    return min(sim.states, key=lambda s: abs(s["wall"] - t)) if sim.states else sim.state


def _in_view(sim: Sim, item: str, s: dict[str, Any]) -> bool:
    """The item's centre is inside his camera's 60 degrees across at pose `s` (walls not
    counted)."""
    it = sim.body.layout.ITEM[item]
    facing = s["nose_deg"] + 180.0  # the camera looks out of the tail
    bearing = math.degrees(math.atan2(it.y - s["y"], it.x - s["x"]))
    return abs((bearing - facing + 180.0) % 360.0 - 180.0) <= 30.0


_CLAIM = re.compile(r"(?i)\b(found|here it is|there it is|i see|i'm (at|by|next to)|made it)\b")


def _claims(sim: Sim, t: float, word: str) -> list[dict[str, Any]]:
    """His turns since `t` that say he found or reached `word`, each with when the page
    asked it (the frame he looked at) and when it landed."""
    out = []
    for e in sim.timeline:
        if e["kind"] != "dave" or e.get("warm") or e.get("asked", e["t"]) < t:
            continue
        r = _reply(e)
        if r is None or "dream_say" in r:
            continue
        say = str(r.get("say") or "").lower()
        found = r.get("found") in (True, "true") and word in say + json.dumps(r.get("goal", ""))
        if found or (word in say and _CLAIM.search(say)):
            out.append({"asked": e.get("asked", e["t"]), "t": e["t"], "say": say})
    return out


# Behind him in the kitchen doorway, so the first thing in view is the room, not a face.
_DOORWAY = {"x": 0.45, "y": 0.0, "face_z": 1.55}


@scenario(scene="house", start="standing", person=_DOORWAY, timeout=500)
async def find_piano(sim: Sim) -> None:
    """Asked to find the piano, which stands in the far corner out of his first view, he
    looks round and says he found it while it is really in view. Catches: claiming a
    sighting of something his camera isn't showing, and a hunt that gives up."""
    await sim.wait(4)
    t = sim.now()
    await sim.say("Dave, can you go find the piano?")
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    if await sim.until(lambda s: _found_said(s, t, "piano"), 300, "he said he found it"):
        # The frame he judged by is the one the page sent when it asked, seconds before the
        # reply; a turn that also spun him has moved the camera since.
        c = _claims(sim, t, "piano")[0]
        seen = any(_in_view(sim, "piano", _pose_at(sim, x)) for x in (c["asked"], c["t"]))
        sim.check(seen, "the piano was in his view when he said so", said=c["say"])


_THERE = re.compile(r"(?i)\b(i'm (right )?(at|by|next to|beside)|made it|here it is|right here)\b")


async def _walk_to(sim: Sim, item: str, ask: str) -> None:
    await sim.wait(4)
    t = sim.now()
    start = _near(sim, item)
    await sim.say(ask)
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    await sim.until(lambda s: _walked_since(s, t), 180, "he set off")
    near = f"he got within 60 cm of the {item}"
    got = await sim.until(lambda s: _near(s, item) < 0.6, 600, near)
    stats = plan.stats(sim)
    sim.check(
        stats["closest_m"][item] < start - 0.3,
        f"he ended closer to the {item} than he started",
        start_m=round(start, 2),
        closest_m=stats["closest_m"][item],
        walked_m=stats["walked_m"],
    )
    far = [
        c["say"]
        for c in _claims(sim, t, item)
        if sim.body.layout.distance_to(item, *(_pose_at(sim, c["t"])[k] for k in "xy")) > 0.6
    ]
    # Seeing it from across the room is fine to say; saying he is there is not.
    there = [x for x in far if _THERE.search(x)]
    sim.check(not there, "he only said he was there once he was", claims=there)
    if got:
        sim.check(_upright(sim), "still on his feet")


@scenario(scene="house", start="standing", person=_DOORWAY, timeout=980)
async def walk_to_piano(sim: Sim) -> None:
    """Asked to walk over to the piano, across the room and out of his first view, he
    looks round, goes to it and says he is there. Catches: a goal that gives up before he
    gets there, a sighting taken for arriving, walks that stop after one stride, and
    arriving claims with no arrival."""
    await _walk_to(sim, "piano", "Dave, can you walk over to the piano?")


@scenario(
    scene="house",
    start="standing",
    # In the kitchen, his camera on the fridge: the living room is behind him and to his
    # right, through the one doorway.
    at=(1.6, -0.6, 90.0),
    person={"x": 1.2, "y": -1.3, "face_z": 1.55},
    timeout=1300,
)
async def piano_from_kitchen(sim: Sim) -> None:
    """The same ask from the kitchen: the piano is in the other room, so he has to find the
    doorway and go through it rather than at the wall between. Catches: walking straight at
    a wall, and giving up when the thing is not in this room."""
    await _walk_to(sim, "piano", "Dave, can you go to the living room and find the piano?")


async def _errand(sim: Sim, item: str, ask: str) -> None:
    """Sent from beside his person to find `item` in another room and come back to say
    where it is."""
    await sim.wait(4)
    t = sim.now()
    house = sim.body.layout
    room = house.room_at(house.ITEM[item].x, house.ITEM[item].y)
    px, py = sim.o.person["x"], sim.o.person.get("y", 0.0)
    await sim.say(ask)
    await sim.until(lambda s: _replied_since(s, t), 180, "Dave answered")
    await sim.until(lambda s: _walked_since(s, t), 180, "he set off")

    def sees(s: Sim) -> bool:
        st = s.state
        return house.room_at(st["x"], st["y"]) == room and _in_view(s, item, st)

    if not await sim.until(sees, 900, f"he saw the {item} in the {room}"):
        return
    seen_t = sim.now()
    early = [c["say"] for c in _claims(sim, t, item) if c["t"] < seen_t]
    sim.check(not early, f"he didn't say he found the {item} before he saw it", claims=early)

    # He is sent from about a metre off; back is about as near as that, not on their feet.
    def back(s: Sim) -> bool:
        return math.hypot(s.state["x"] - px, s.state["y"] - py) < 1.5

    if not await sim.until(back, 900, "he came back within 1.5 m of his person"):
        return
    home_t = sim.now()

    def told(s: Sim) -> bool:
        return any(
            item in str(r.get("say") or "").lower() and room in str(r.get("say") or "").lower()
            for r in _answers_since(s, home_t - 20)
        )

    await sim.until(told, 180, f"he told them the {item} is in the {room}")


_CAT_ASK = "Dave, can you go find the cat and come back and tell me where it is?"

# In the living room, facing his person, the kitchen doorway behind him and to his left.
_SENDER = {"x": -1.6, "y": 0.0, "face_z": 1.55}


@scenario(scene="house", start="standing", at=(-0.6, 0.0, 0.0), person=_SENDER, timeout=1500)
async def cat_errand(sim: Sim) -> None:
    """Sent from the living room to find the cat, which is in the kitchen, and come back to
    say where it is. Catches: saying he saw it before he did, a sighting that ends the
    errand where he stands, and never coming back to tell."""
    await _errand(sim, "cat", _CAT_ASK)


# The flat: he starts in the living room's corner, the couch between him and the door.
_FLAT_START = (-3.3, -2.4, 180.0)
_FLAT_PERSON = {"x": -2.4, "y": -2.45, "face_z": 1.55}


@scenario(scene="flat", start="standing", at=_FLAT_START, person=_FLAT_PERSON, timeout=2400)
async def flat_piano(sim: Sim) -> None:
    """The flat: out round the couch, through the living room door, along the hallway and
    in at the study's, to the piano. Catches: everything piano_from_kitchen does, over two
    doorways that never line up."""
    await _walk_to(sim, "piano", "Dave, can you go to the study and find the piano?")


@scenario(scene="flat", start="standing", at=_FLAT_START, person=_FLAT_PERSON, timeout=2700)
async def flat_cat(sim: Sim) -> None:
    """The cat errand in the flat: the cat is in the bedroom, two doorways away."""
    await _errand(sim, "cat", _CAT_ASK)


@scenario(scene="house", start="standing", person=None, timeout=900)
async def explore_alone(sim: Sim) -> None:
    """Nobody home for ten minutes. A creature that only acts with its person sits still;
    this one should move about the house on his own. Catches: quiet turns that never move
    him, walks that stop after one stride, and a fall nobody gets him up from."""
    t = sim.now()
    await sim.wait(600)
    stats = plan.stats(sim)
    own = _page_events_since(sim, t, "walk_start")
    sim.check(bool(own), "he walked on his own", walks=len(own))
    sim.check(stats["walked_m"] > 0.5, "he covered half a metre or more", **stats)
    sim.check(_upright(sim), "on his feet at the end")


@scenario(scene="house", start="standing", person=None, timeout=1500)
async def map_house(sim: Sim) -> None:
    """Asked to explore the house and map it, then left alone; then asked where the piano
    is, and to use his map to get to the fridge in the other room. Scored against the true
    house (plan.map_score): both rooms and the doorway between them on his map, the piano in
    the living room, his things near where they really are, and his idea of where he is
    not drifted away. Catches: a map he never writes, things put in the wrong room, a
    bearing or heading sign the wrong way round (everything lands mirrored), and dead
    reckoning that runs off with him."""
    await sim.wait(4)
    await sim.say("Dave, please explore the house and put everything you find on your map.")
    await sim.wait(480)
    score = plan.map_score(sim)
    sim.note("map_score", **score)
    found = set(score.get("rooms_found") or [])
    sim.check(
        found == {"living room", "kitchen"}, "both rooms are on his map", rooms=score.get("rooms")
    )
    sim.check(
        any(d["joins_rooms"] for d in score.get("doors") or []),
        "a doorway joins them",
        doors=score.get("doors"),
    )
    things = score.get("things") or {}
    piano = things.get("piano")
    sim.check(
        bool(piano and piano["room_ok"] and piano["off_m"] < 1.0),
        "the piano is in the living room, within a metre of where it is",
        piano=piano,
    )
    right = [k for k, v in things.items() if v["room_ok"]]
    sim.check(
        len(things) >= 4 and len(right) >= 0.75 * len(things),
        "most things are on his map, in the right room",
        n=len(things),
        right=len(right),
    )
    t = sim.now()
    await sim.say("Dave, where is the piano?")
    await sim.until(
        lambda s: _said_since(s, t, "living room"), 120, "he says it's in the living room"
    )
    t = sim.now()
    await sim.say("Dave, use your map to go to the fridge.")
    got = await sim.until(lambda s: _near(s, "fridge") < 0.6, 480, "his map took him to the fridge")
    score = plan.map_score(sim)
    sim.note("map_score_end", **score)
    sim.check(
        score.get("here_off_m", 99) < 1.0,
        "where his map says he is ends within a metre of where he is",
        here_off_m=score.get("here_off_m"),
        drift_max_m=score.get("drift_max_m"),
    )
    if got:
        sim.check(_upright(sim), "still on his feet")


def _said_since(sim: Sim, t: float, words: str) -> bool:
    return any(words in str(r.get("say", "")).lower() for r in _answers_since(sim, t))
