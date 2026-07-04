"""JPet v2 drive math (docs/proposed/JPET_V2_PLAN.md) — pure, no DB, no clock.

Proves the positive "happy meters": awake time never decays them (no neglect), a nap
gently recovers energy, clamping holds, a still clock is a no-op, and mood is always a
positive label (no sad/hungry stakes).
"""

from jbrain.jpet.service import Drives, decayed, mood_of

FULL = Drives(food=80.0, energy=80.0, fun=70.0, love=70.0)


def test_awake_time_never_decays_the_meters() -> None:
    # v2: the pet is never neglected — an hour awake changes nothing.
    assert decayed(FULL, 3600.0, asleep=False) == FULL
    assert decayed(FULL, 3600.0 * 24, asleep=False) == FULL


def test_zero_and_negative_dt_is_a_noop() -> None:
    assert decayed(FULL, 0.0, asleep=False) == FULL
    assert decayed(FULL, -5.0, asleep=True) == FULL


def test_asleep_recovers_energy_only() -> None:
    rested = Drives(food=80.0, energy=50.0, fun=70.0, love=70.0)
    after = decayed(rested, 3600.0, asleep=True)
    assert after.energy == 80.0  # 50 + 30/h
    assert after.food == 80.0 and after.fun == 70.0 and after.love == 70.0  # rest untouched


def test_energy_recovery_clamps_at_100() -> None:
    nearly = Drives(food=50.0, energy=99.0, fun=50.0, love=50.0)
    assert decayed(nearly, 3600.0 * 100, asleep=True).energy == 100.0


def test_mood_is_always_positive() -> None:
    assert mood_of(FULL, asleep=True) == "sleepy"
    assert mood_of(Drives(90, 90, 90, 90), asleep=False) == "excited"
    assert mood_of(Drives(60, 60, 60, 60), asleep=False) == "happy"
    # even an empty-meter pet reads positive — never sad or hungry
    assert mood_of(Drives(0, 0, 0, 0), asleep=False) == "playful"
    assert mood_of(Drives(10, 95, 95, 95), asleep=False) in {"happy", "excited", "playful"}
