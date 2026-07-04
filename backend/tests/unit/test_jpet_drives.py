"""JPet drive math (docs/plans/JPET_PLAN.md §4) — pure, no DB, no clock.

Proves the "needs" model: awake decay, asleep energy recovery + near-freeze of the
rest, clamping, a no-op on a still clock, and the mood thresholds.
"""

from jbrain.jpet.service import (
    ASLEEP_DECAY_FACTOR,
    DECAY_PER_HOUR,
    ENERGY_RECOVER_PER_HOUR,
    Drives,
    decayed,
    mood_of,
)

FULL = Drives(food=80.0, energy=80.0, fun=70.0, love=70.0)


def test_awake_decay_drops_each_need_at_its_hourly_rate() -> None:
    after = decayed(FULL, 3600.0, asleep=False)
    assert after.food == 80.0 - DECAY_PER_HOUR["food"]
    assert after.energy == 80.0 - DECAY_PER_HOUR["energy"]
    assert after.fun == 70.0 - DECAY_PER_HOUR["fun"]
    assert after.love == 70.0 - DECAY_PER_HOUR["love"]


def test_zero_and_negative_dt_is_a_noop() -> None:
    assert decayed(FULL, 0.0, asleep=False) == FULL
    assert decayed(FULL, -5.0, asleep=False) == FULL


def test_asleep_recovers_energy_and_nearly_freezes_the_rest() -> None:
    # Start energy below full so the +30/h recovery is observable (not clamped).
    rested = Drives(food=80.0, energy=50.0, fun=70.0, love=70.0)
    after = decayed(rested, 3600.0, asleep=True)
    assert after.energy == 50.0 + ENERGY_RECOVER_PER_HOUR
    # non-energy needs decay at the reduced asleep factor, not the full rate
    assert after.food == 80.0 - DECAY_PER_HOUR["food"] * ASLEEP_DECAY_FACTOR


def test_clamped_to_zero_and_one_hundred() -> None:
    empty = Drives(food=1.0, energy=1.0, fun=1.0, love=1.0)
    assert decayed(empty, 3600.0 * 100, asleep=False) == Drives(0.0, 0.0, 0.0, 0.0)
    nearly_full = Drives(food=50.0, energy=99.0, fun=50.0, love=50.0)
    assert decayed(nearly_full, 3600.0 * 100, asleep=True).energy == 100.0


def test_mood_thresholds() -> None:
    assert mood_of(FULL, asleep=True) == "sleepy"
    assert mood_of(Drives(90, 90, 90, 90), asleep=False) == "happy"
    assert mood_of(Drives(60, 60, 60, 60), asleep=False) == "neutral"
    assert mood_of(Drives(30, 30, 30, 30), asleep=False) == "sad"
    # a starving-but-otherwise-fine pet still reads hungry (short-circuits average)
    assert mood_of(Drives(10, 95, 95, 95), asleep=False) == "hungry"
