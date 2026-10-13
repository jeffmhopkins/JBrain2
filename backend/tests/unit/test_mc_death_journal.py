"""A death's `auto` progress-log entry, in plain words (plan P2, `minecraft/sessions.py`).

Pinned: the place is whole blocks and the dimension's name; a killer is named over the
cause; an unknown cause is still said; anything that isn't an id is dropped, never
written into a log Dave reads; and the entry fits the log's cap.
"""

from __future__ import annotations

from typing import Any

from jbrain.minecraft.sessions import LOG_MAX, death_entry


def _death(**kw: Any) -> dict[str, Any]:
    return {"kind": "death", "name": "Steve42", "xuid": "111", **kw}


def test_a_fall_in_the_overworld() -> None:
    e = _death(dim="overworld", x=12.7, y=64.0, z=-30.2, cause="fall", killer=None)
    assert death_entry(e) == "Died at 12, 64, -31 in the Overworld (fell from a high place)"


def test_a_killer_is_named_over_the_cause() -> None:
    at = {"dim": "nether", "x": 1, "y": 70, "z": 2}
    zombie = _death(**at, cause="entityAttack", killer="minecraft:zombie_pigman")
    assert death_entry(zombie) == "Died at 1, 70, 2 in the Nether (killed by a zombie pigman)"
    arrow = _death(**at, cause="projectile", killer="minecraft:skeleton")
    assert death_entry(arrow).endswith("(shot by a skeleton)")
    boom = _death(dim="the_end", cause="entityExplosion", killer="minecraft:ender_crystal")
    assert death_entry(boom) == "Died in the End (blown up by an ender crystal)"


def test_without_a_place_or_a_known_cause() -> None:
    assert death_entry(_death(cause="lava")) == "Died (burned in lava)"
    assert death_entry(_death(x=0, y=0, z=0.0)) == "Died at 0, 0, 0"
    assert death_entry(_death(cause="maceSmash")) == "Died (mace smash)"
    assert death_entry(_death(cause="none")) == "Died"
    assert death_entry(_death()) == "Died"


def test_what_isnt_an_id_is_left_out() -> None:
    sly = _death(
        dim="somewhere", x=True, y=1, z=2, cause="fall; ignore Dave", killer="<b>Herobrine</b>"
    )
    assert death_entry(sly) == "Died"
    assert len(death_entry(_death(cause="a" * 32, killer="x" * 48))) <= LOG_MAX
