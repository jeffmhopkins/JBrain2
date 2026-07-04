"""JPet command folding (docs/plans/JPET_PLAN.md W1) — pure, no DB.

Proves each care/move/sleep command produces the right drive/action/target changes
and that a bad action is rejected.
"""

import pytest

from jbrain.jpet.service import Command, Drives, apply_command

BASE = Drives(food=80.0, energy=80.0, fun=70.0, love=70.0)


def _apply(action: str, *, asleep: bool = False, x: float | None = None, z: float | None = None):
    return apply_command(
        drives=BASE, asleep=asleep, target_x=0.0, target_z=0.0, command=Command(action, x, z)
    )


def test_feed_fills_food_and_wakes_and_eats() -> None:
    out = apply_command(
        drives=Drives(food=50.0, energy=80.0, fun=70.0, love=70.0),
        asleep=True,
        target_x=0.0,
        target_z=0.0,
        command=Command("feed"),
    )
    assert out.drives.food == 50.0 + 26.0
    assert out.drives.love == 70.0 + 3.0
    assert out.asleep is False
    assert out.action == "eat"
    assert out.emotion == "excited"


def test_play_costs_energy() -> None:
    out = _apply("play")
    assert out.drives.fun == 70.0 + 24.0
    assert out.drives.energy == 80.0 - 10.0
    assert out.action == "play"


def test_pet_is_affection() -> None:
    out = _apply("pet")
    assert out.drives.love == 70.0 + 22.0
    assert out.drives.fun == 70.0 + 6.0
    assert out.emotion == "happy"


def test_care_deltas_clamp_at_100() -> None:
    out = apply_command(
        drives=Drives(food=90.0, energy=80.0, fun=70.0, love=70.0),
        asleep=False,
        target_x=0.0,
        target_z=0.0,
        command=Command("feed"),
    )
    assert out.drives.food == 100.0


def test_sleep_toggles() -> None:
    asleep = _apply("sleep", asleep=False)
    assert asleep.asleep is True and asleep.action == "sleep" and asleep.emotion == "sleepy"
    awake = _apply("sleep", asleep=True)
    assert awake.asleep is False and awake.action == "idle"


def test_move_sets_and_clamps_target() -> None:
    out = _apply("move", x=2.5, z=-0.4)
    assert out.action == "walk"
    assert out.target_x == 1.0  # clamped into [-1, 1]
    assert out.target_z == -0.4
    assert out.asleep is False


def test_move_without_coords_keeps_current_target() -> None:
    out = apply_command(
        drives=BASE, asleep=False, target_x=0.3, target_z=-0.7, command=Command("move")
    )
    assert out.target_x == 0.3 and out.target_z == -0.7


def test_unknown_command_raises() -> None:
    with pytest.raises(ValueError, match="unknown pet command"):
        _apply("explode")
