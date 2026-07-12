"""JPet ephemeral wall effects — the "turn X <colour>" / "make X bigger" overrides.

Proves the in-memory store folds recolor/resize intents correctly (step + clamp, reset drops
the override) and that `PetOut.of` overlays the store onto the wire shape the wall polls. Pure —
no DB, no HTTP. These effects are never persisted; a reload clears them (tested at the API).
"""

from datetime import UTC, datetime

from jbrain.api.pet import PetOut, _apply_effect, _clamp_scale
from jbrain.jpet.service import PetStateInfo


def _fx() -> dict:
    return {"colors": {}, "scales": {}, "pet_scale": 1.0}


def _state() -> PetStateInfo:
    return PetStateInfo(
        id="p",
        name="Blink",
        domain="general",
        mood="playful",
        emotion="happy",
        speech=None,
        asleep=False,
        pos_x=0,
        pos_z=0,
        target_x=0,
        target_z=0,
        facing=0,
        action="idle",
        color=None,
        objects={"ball": (0.0, 0.35)},
        last_tick_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_recolor_sets_and_default_clears_the_object() -> None:
    fx = _fx()
    _apply_effect(fx, kind="recolor", target="floor", value="blue")
    assert fx["colors"] == {"floor": "blue"}
    _apply_effect(fx, kind="recolor", target="floor", value="default")  # "turn the floor normal"
    assert fx["colors"] == {}


def test_resize_object_steps_and_reset_drops_it() -> None:
    fx = _fx()
    _apply_effect(fx, kind="resize", target="bed", value="grow")
    _apply_effect(fx, kind="resize", target="bed", value="grow")
    assert fx["scales"]["bed"] == _clamp_scale(1.25 * 1.25)
    _apply_effect(fx, kind="resize", target="bed", value="reset")  # "make the bed normal"
    assert "bed" not in fx["scales"]


def test_resize_robot_uses_pet_scale_and_clamps() -> None:
    fx = _fx()
    for _ in range(20):  # would run away past the ceiling without the clamp
        _apply_effect(fx, kind="resize", target="robot", value="grow")
    assert fx["pet_scale"] == _clamp_scale(1e9)  # pinned at the max
    _apply_effect(fx, kind="resize", target="robot", value="reset")
    assert fx["pet_scale"] == 1.0
    for _ in range(20):
        _apply_effect(fx, kind="resize", target="robot", value="shrink")
    assert fx["pet_scale"] == _clamp_scale(0.0)  # pinned at the min


def test_petout_overlays_the_effects_and_defaults_to_empty() -> None:
    info = _state()
    bare = PetOut.of(info)
    assert bare.object_colors == {} and bare.object_scales == {} and bare.pet_scale == 1.0
    fx = {"colors": {"floor": "blue"}, "scales": {"bed": 1.5}, "pet_scale": 2.0}
    with_fx = PetOut.of(info, fx)
    assert with_fx.object_colors == {"floor": "blue"}
    assert with_fx.object_scales == {"bed": 1.5}
    assert with_fx.pet_scale == 2.0
