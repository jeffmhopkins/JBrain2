"""The live scan's depth limit as the backend says it (§M25): the same 16 blocks each way,
cut at the floor and ceiling, as the pack's `scanBox` (jbrain-sim/rules.test.mjs pins the
pack's side with these numbers)."""

from __future__ import annotations

import pytest

from jbrain.minecraft import livescan


@pytest.mark.parametrize(
    ("dim", "y", "want"),
    [
        ("overworld", -40, (-56, -24)),
        ("overworld", 63.9, (47, 79)),
        ("overworld", -52, (-64, -36)),
        ("overworld", -60, (-64, -44)),
        ("overworld", 310, (294, 319)),
        ("overworld", -64, (-64, -48)),
        ("overworld", 319, (303, 319)),
        ("overworld", -90, (-64, -64)),
        ("overworld", 400, (319, 319)),
        ("nether", 120, (104, 127)),
        ("the_end", 5, (0, 21)),
        ("somewhere", 0, (-16, 16)),
    ],
)
def test_the_scan_reaches_16_up_and_down_cut_at_the_world(dim, y, want) -> None:
    assert livescan.y_range(dim, y) == want


def test_a_scan_is_keyed_like_the_pack() -> None:
    assert livescan.key("  BlockyFox ") == "blockyfox"
