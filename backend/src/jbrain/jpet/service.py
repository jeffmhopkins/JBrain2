"""JPet drive model — DTOs + the pure "needs" math (docs/plans/JPET_PLAN.md §4).

The pet's intelligence is two independent things; this module is the first: the
Sims-style drives are *numbers on a timer*, not ML. `food`/`energy`/`fun`/`love`
are 0–100 satisfaction (100 = fully satisfied). They decay while the pet is awake
and mood is a pure function of them. Everything here is deterministic and side-
effect-free so it unit-tests without a database or a clock — the repo/tick call
these to advance the stored row.
"""

from dataclasses import dataclass, replace
from datetime import datetime

# Points lost per hour of wakefulness (energy is the exception — it recovers while
# asleep). Gentle by design: a cared-for pet coasts for hours, and neglect shows
# over a day, not minutes. Tunable; the tick cadence is independent of these.
DECAY_PER_HOUR = {"food": 6.0, "energy": 4.0, "fun": 5.0, "love": 3.0}
ENERGY_RECOVER_PER_HOUR = 30.0
# While asleep the non-energy needs nearly freeze (a sleeping pet still gets a
# little hungry, but slowly) — mirrors the mockups' sleep behaviour.
ASLEEP_DECAY_FACTOR = 0.15

DRIVE_NAMES = ("food", "energy", "fun", "love")


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


@dataclass(frozen=True)
class Drives:
    """The four needs, 0–100 satisfaction (higher is better)."""

    food: float
    energy: float
    fun: float
    love: float

    @property
    def average(self) -> float:
        return (self.food + self.energy + self.fun + self.love) / 4


def decayed(drives: Drives, dt_seconds: float, *, asleep: bool) -> Drives:
    """Advance the drives by `dt_seconds` of elapsed time. Awake: each need falls at
    its hourly rate. Asleep: energy recovers, the rest nearly freeze. Clamped to
    0–100. `dt_seconds <= 0` is a no-op (a clock that didn't move)."""
    if dt_seconds <= 0:
        return drives
    hours = dt_seconds / 3600.0
    factor = ASLEEP_DECAY_FACTOR if asleep else 1.0
    energy = (
        drives.energy + ENERGY_RECOVER_PER_HOUR * hours
        if asleep
        else drives.energy - DECAY_PER_HOUR["energy"] * hours
    )
    return Drives(
        food=_clamp(drives.food - DECAY_PER_HOUR["food"] * hours * factor),
        energy=_clamp(energy),
        fun=_clamp(drives.fun - DECAY_PER_HOUR["fun"] * hours * factor),
        love=_clamp(drives.love - DECAY_PER_HOUR["love"] * hours * factor),
    )


def mood_of(drives: Drives, *, asleep: bool) -> str:
    """The materialized mood label — a pure function of the drives (same thresholds
    as the mockups). `hungry` short-circuits above the average so a starving-but-
    otherwise-fine pet still reads as hungry."""
    if asleep:
        return "sleepy"
    if drives.food < 25:
        return "hungry"
    avg = drives.average
    if avg > 70:
        return "happy"
    if avg > 45:
        return "neutral"
    return "sad"


@dataclass(frozen=True)
class PetStateInfo:
    """A read of the pet row for the API/tick layers. Wire/serialization shapes are
    built from this; the drives live in the `drives` sub-DTO."""

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
    last_tick_at: datetime
    updated_at: datetime

    def with_drives(self, drives: Drives, *, asleep: bool | None = None) -> "PetStateInfo":
        """A copy with new drives and a recomputed mood — used by the tick to project
        the next state before it's written."""
        sleep = self.asleep if asleep is None else asleep
        return replace(self, drives=drives, asleep=sleep, mood=mood_of(drives, asleep=sleep))
