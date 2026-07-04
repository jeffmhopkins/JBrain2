"""JPet play model — DTOs + the pure play/room math (docs/proposed/JPET_V2_PLAN.md).

v2 pivots the pet from a Tamagotchi (drives that *decay* toward hunger/neglect) to a
positive, command-and-response play companion for 3–4-year-olds. Two things still live
here, both pure and side-effect-free so they unit-test with no DB or clock:

1. **Drives** (`food`/`energy`/`fun`/`love`, 0–100) are now *happy meters*, not a
   countdown: playing raises them and they never decay toward sad. `mood_of` only ever
   returns a positive label — the pet is always willing to play.
2. **The action script** — the pet does short, ordered sequences of bounded *primitive*
   actions ("chase the ball, then spin, then sit"). The vocabulary is a fixed allow-list;
   `clean_script` guarantees every script is short, in-vocabulary, affordance-grounded,
   and always-terminating; `settle_script` computes the pet's + room's resting state after
   a script runs (the wall animates the journey between server updates).
"""

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

# ── Vocabulary (the fixed allow-list the LLM and the buttons may draw from) ────────────
# In-place expressive primitives (no target, cosmetic) and the terminating rest poses.
EXPRESSIVE = ("dance", "spin", "jump", "wave", "wiggle", "nod", "beep", "hide")
TERMINAL = ("idle", "sit", "sleep")
# Primitives that relocate the pet and/or touch a room object.
TARGETED = ("go_to", "come_here", "chase", "look_at", "pick_up", "put_down", "carry_to")
SLEEP_ACTIONS = ("sleep", "wake")
# Everything the LLM may emit as a script step, plus `walk` which only the wander uses.
PRIMITIVES = EXPRESSIVE + TARGETED + SLEEP_ACTIONS + ("sit", "idle")
ACTIONS = tuple(dict.fromkeys(PRIMITIVES + ("walk",)))  # stored `action` set (de-duped)

EMOTIONS = ("happy", "excited", "curious", "sleepy", "silly", "scared")

# Room props the pet can target. `ball` is the only movable one (it can be carried); the
# rest are furniture at a fixed home. Coordinates are the normalized floor [-1, 1]² the
# clients scale to their room (x = right, z = depth/away from the viewer).
OBJECT_HOMES: dict[str, tuple[float, float]] = {
    "ball": (0.0, 0.35),
    "bed": (-0.72, -0.7),
    "toy_box": (0.72, -0.7),
    "food_bowl": (0.72, 0.72),
    "ball_pit": (-0.72, 0.72),
    "light_switch": (0.0, -0.92),  # on the back wall; targeting it toggles the lights
}
OBJECTS = tuple(OBJECT_HOMES)
MOVABLE = frozenset({"ball"})  # only these can be picked up / carried

# Named floor spots a kid request or the LLM can send the pet to.
LOCATIONS: dict[str, tuple[float, float]] = {
    "corner_ne": (0.85, -0.85),
    "corner_nw": (-0.85, -0.85),
    "corner_se": (0.85, 0.85),
    "corner_sw": (-0.85, 0.85),
    "center": (0.0, 0.0),
    "near_child": (0.0, 0.82),
}

# Every script is capped short (attention span + always-terminating) and ends at rest.
MAX_SCRIPT_STEPS = 6
MIN_STEP_MS = 200
MAX_STEP_MS = 3000

DRIVE_NAMES = ("food", "energy", "fun", "love")


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _clamp_unit(v: float) -> float:
    return max(-1.0, min(1.0, v))


@dataclass(frozen=True)
class Drives:
    """The four happy meters, 0–100 (higher is better). They no longer decay."""

    food: float
    energy: float
    fun: float
    love: float

    @property
    def average(self) -> float:
        return (self.food + self.energy + self.fun + self.love) / 4


def decayed(drives: Drives, dt_seconds: float, *, asleep: bool) -> Drives:
    """v2: the meters do NOT decay — the pet is never neglected. Awake time is a no-op;
    the one time-based change kept is that sleeping gently *recovers* energy, so a nap is
    rewarding. Clamped 0–100. `dt_seconds <= 0` is a no-op (a clock that didn't move)."""
    if dt_seconds <= 0 or not asleep:
        return drives
    hours = dt_seconds / 3600.0
    return replace(drives, energy=_clamp(drives.energy + 30.0 * hours))


def mood_of(drives: Drives, *, asleep: bool) -> str:
    """The materialized mood — a pure function of the meters, always POSITIVE (no sad/
    hungry stakes). A well-played-with pet is `excited`; a calm one is `happy`; a quiet
    one is `playful`; asleep is `sleepy`."""
    if asleep:
        return "sleepy"
    avg = drives.average
    if avg > 75:
        return "excited"
    if avg > 45:
        return "happy"
    return "playful"


# ── Play commands (the big kid buttons) → canned scripts ──────────────────────────────
# Each one-tap button expands to a short, safe, terminating script. Drive bumps are pure
# reward (play always raises meters, never lowers below a floor). Mirrors the phone UI.
def _script(*steps: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(s) for s in steps]


CANNED_SCRIPTS: dict[str, list[dict[str, Any]]] = {
    "dance": _script(
        {"action": "dance", "duration_ms": 2200, "emotion": "silly"}, {"action": "sit"}
    ),
    "spin": _script(
        {"action": "spin", "duration_ms": 1400, "emotion": "excited"}, {"action": "idle"}
    ),
    "jump": _script(
        {"action": "jump", "duration_ms": 900, "emotion": "excited"}, {"action": "idle"}
    ),
    "wave": _script(
        {"action": "wave", "duration_ms": 1200, "emotion": "happy"}, {"action": "idle"}
    ),
    "wiggle": _script(
        {"action": "wiggle", "duration_ms": 1400, "emotion": "silly"}, {"action": "idle"}
    ),
    "chase": _script(
        {"action": "chase", "target": "ball", "emotion": "excited"},
        {"action": "wiggle", "duration_ms": 900},
        {"action": "sit"},
    ),
    "hide": _script(
        {"action": "hide", "destination": "corner_nw", "emotion": "curious"}, {"action": "sit"}
    ),
    "beep": _script({"action": "beep", "duration_ms": 700, "emotion": "silly"}, {"action": "idle"}),
    "come": _script(
        {"action": "come_here", "emotion": "happy"},
        {"action": "wave", "duration_ms": 900},
        {"action": "idle"},
    ),
    "sleep": _script(
        {"action": "go_to", "target": "bed", "emotion": "sleepy"}, {"action": "sleep"}
    ),
    "wake": _script(
        {"action": "wake", "emotion": "happy"},
        {"action": "wiggle", "duration_ms": 800},
        {"action": "idle"},
    ),
    "eat": _script(
        {"action": "go_to", "target": "food_bowl", "emotion": "happy"},
        {"action": "nod", "duration_ms": 1200},
        {"action": "sit"},
    ),
    "lights": _script(
        {"action": "go_to", "target": "light_switch", "emotion": "curious"},
        {"action": "jump", "duration_ms": 700},
        {"action": "idle"},
    ),
}
# Play reward applied on any command (bounded, one-directional): fun + a bit of love.
PLAY_REWARD = {"fun": 8.0, "love": 4.0, "energy": -2.0}
BUTTON_ACTIONS = frozenset(CANNED_SCRIPTS)


@dataclass(frozen=True)
class Step:
    """One bounded action in a script. `target`/`destination` are read only for targeted
    primitives; `duration_ms` bounds an in-place motion; `emotion` colours the pose."""

    action: str
    target: str | None = None
    destination: str | None = None
    duration_ms: int | None = None
    emotion: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action}
        if self.target is not None:
            out["target"] = self.target
        if self.destination is not None:
            out["destination"] = self.destination
        if self.duration_ms is not None:
            out["duration_ms"] = self.duration_ms
        if self.emotion is not None:
            out["emotion"] = self.emotion
        return out


def _coerce_step(raw: Any, *, objects: dict[str, tuple[float, float]]) -> Step | None:
    """Validate one raw step against the allow-lists. Returns None (drop the step) if the
    action is unknown, or a targeted action references an object/location not in the room
    — the affordance check that keeps a hallucinated target from wedging the runner."""
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in PRIMITIVES:
        return None
    target = raw.get("target") if raw.get("target") in objects else None
    dest = raw.get("destination") if raw.get("destination") in LOCATIONS else None
    # A targeted move with no valid target/destination is meaningless — drop it (except
    # chase/pick_up/carry which default to the ball / current carry, handled in settle).
    if action in ("go_to", "look_at") and target is None and dest is None:
        return None
    dur = raw.get("duration_ms")
    duration = (
        max(MIN_STEP_MS, min(MAX_STEP_MS, int(dur))) if isinstance(dur, (int, float)) else None
    )
    emotion = raw.get("emotion") if raw.get("emotion") in EMOTIONS else None
    return Step(
        action=str(action), target=target, destination=dest, duration_ms=duration, emotion=emotion
    )


def clean_script(raw: Any, *, objects: dict[str, tuple[float, float]]) -> list[Step]:
    """Coerce the model's (or a client's) raw step list into a safe, bounded, always-
    terminating script: drop unknown/ungrounded steps, cap the length, and guarantee the
    last step is a rest pose (append `sit` if not). An empty/garbage input yields a lone
    `idle` so the pet always has a valid, terminating script."""
    steps: list[Step] = []
    if isinstance(raw, list):
        for item in raw:
            step = _coerce_step(item, objects=objects)
            if step is not None:
                steps.append(step)
            if len(steps) >= MAX_SCRIPT_STEPS:
                break
    if not steps:
        return [Step(action="idle")]
    if steps[-1].action not in TERMINAL:
        if len(steps) >= MAX_SCRIPT_STEPS:
            steps[-1] = Step(action="sit")
        else:
            steps.append(Step(action="sit"))
    return steps


def canned_script(action: str, *, objects: dict[str, tuple[float, float]]) -> list[Step]:
    """The safe script for a one-tap kid button (dance/chase/hide/…). Runs through the
    same cleaner so buttons and LLM output share one bounding path."""
    return clean_script(CANNED_SCRIPTS.get(action, []), objects=objects)


@dataclass(frozen=True)
class Settled:
    """The pet's + room's resting state after a script runs — pure, so it unit-tests
    without a DB. The wall animates the transition from the previous state to this one."""

    pos_x: float
    pos_z: float
    facing: float
    asleep: bool
    carrying: str | None
    lights_on: bool
    objects: dict[str, tuple[float, float]]
    action: str  # the final resting action (a TERMINAL value)


def _target_pos(
    step: Step, *, objects: dict[str, tuple[float, float]], pos: tuple[float, float]
) -> tuple[float, float] | None:
    """Where a step sends the pet, or None if it doesn't move it. `chase`/`carry_to`
    default their target sensibly; a destination location wins over an object target."""
    if step.destination in LOCATIONS:
        return LOCATIONS[step.destination]
    if step.action in ("go_to", "look_at", "chase", "pick_up") and step.target in objects:
        return objects[step.target]
    if step.action == "chase":
        return objects.get("ball", pos)
    if step.action == "come_here":
        return LOCATIONS["near_child"]
    if step.action == "hide":
        return LOCATIONS.get(step.destination or "corner_nw", LOCATIONS["corner_nw"])
    return None


def settle_script(
    *,
    pos_x: float,
    pos_z: float,
    facing: float,
    asleep: bool,
    carrying: str | None,
    lights_on: bool,
    objects: dict[str, tuple[float, float]],
    script: list[Step],
) -> Settled:
    """Fold a whole script into the pet's + room's final resting state (pure arithmetic —
    no per-frame stepping on the server; the wall plays out the motion). Tracks the pet's
    running position, a carried object (which follows the pet), pick_up/put_down/carry,
    lights toggles, and sleep. The final `action` is the last step's terminal pose."""
    pos = (_clamp_unit(pos_x), _clamp_unit(pos_z))
    facing_out = facing
    objs = dict(objects)
    held = carrying if carrying in objs else None
    sleeping = asleep
    lights = lights_on
    for step in script:
        a = step.action
        moved = _target_pos(step, objects=objs, pos=pos)
        if moved is not None:
            want = (_clamp_unit(moved[0]), _clamp_unit(moved[1]))
            dx, dz = want[0] - pos[0], want[1] - pos[1]
            if abs(dx) > 1e-6 or abs(dz) > 1e-6:
                facing_out = math.atan2(dx, dz)
            pos = want
        if a == "pick_up":
            obj = step.target if step.target in MOVABLE else "ball"
            if obj in objs:
                held = obj
        elif a == "carry_to":
            if held is not None:
                objs[held] = pos
        elif a == "put_down":
            if held is not None:
                objs[held] = pos
                held = None
        elif a == "go_to" and step.target == "light_switch":
            lights = not lights
        if held is not None:  # a carried object rides along with the pet
            objs[held] = pos
        if a == "sleep":
            sleeping = True
        elif a == "wake":
            sleeping = False
    final = script[-1].action if script else "idle"
    if final not in TERMINAL:
        final = "sleep" if sleeping else "idle"
    return Settled(
        pos_x=pos[0],
        pos_z=pos[1],
        facing=facing_out,
        asleep=sleeping,
        carrying=held,
        lights_on=lights,
        objects=objs,
        action=final,
    )


def apply_play_reward(drives: Drives) -> Drives:
    """Bump the happy meters for any play command — bounded and one-directional (fun/love
    up, a nudge of energy spent), then clamped. There is no punishment path."""
    return Drives(
        food=_clamp(drives.food + PLAY_REWARD.get("food", 0.0)),
        energy=_clamp(drives.energy + PLAY_REWARD.get("energy", 0.0)),
        fun=_clamp(drives.fun + PLAY_REWARD.get("fun", 0.0)),
        love=_clamp(drives.love + PLAY_REWARD.get("love", 0.0)),
    )


@dataclass(frozen=True)
class PetStateInfo:
    """A read of the pet row for the API/tick layers. Wire shapes build from this."""

    id: str
    name: str
    domain: str
    drives: Drives
    mood: str
    emotion: str
    speech: str | None
    asleep: bool
    pos_x: float
    pos_z: float
    target_x: float
    target_z: float
    facing: float
    action: str
    script: list[dict[str, Any]] = field(default_factory=list)
    script_started_at: datetime | None = None
    carrying: str | None = None
    lights_on: bool = True
    objects: dict[str, tuple[float, float]] = field(default_factory=dict)
    last_tick_at: datetime | None = None
    updated_at: datetime | None = None

    def with_drives(self, drives: Drives, *, asleep: bool | None = None) -> "PetStateInfo":
        """A copy with new drives and recomputed mood — used by the tick to project the
        next state before it's written."""
        sleep = self.asleep if asleep is None else asleep
        return replace(self, drives=drives, asleep=sleep, mood=mood_of(drives, asleep=sleep))
