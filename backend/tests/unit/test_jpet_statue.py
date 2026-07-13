"""The JPet field "build a statue of X" generator (jbrain.jpet.brain).

The LLM designs the subject as coloured PRIMITIVES; the host voxelizes them into a hollow-shell
cube model. This proves the host-side pieces: primitive validation (drop malformed / off-type
shapes, normalise colour), the voxelizer (fills a shape, keeps only the surface shell), and the
end-to-end call raising on an empty model. The LLM is faked via a stub router.
"""

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from jbrain.jpet.brain import (
    STATUE_GRID,
    Voxel,
    _clean_primitives,
    _statue_system_prompt,
    statue_voxels,
    voxelize,
)


class _StubRouter:
    """Returns a preset `parsed` object and records the call, standing in for the real router."""

    def __init__(self, parsed: Any) -> None:
        self._parsed = parsed
        self.calls: list[tuple[str, str, int]] = []

    async def complete(
        self,
        task: str,
        *,
        system: str,
        user_text: str,
        json_schema: Any = None,
        max_tokens: int = 0,
    ) -> Any:
        self.calls.append((task, user_text, max_tokens))
        return SimpleNamespace(parsed=self._parsed)


def _box(cx: float, cy: float, cz: float, s: float, c: str = "#ff8800") -> dict[str, Any]:
    return {"type": "box", "cx": cx, "cy": cy, "cz": cz, "sx": s, "sy": s, "sz": s, "c": c}


async def test_statue_voxels_runs_the_high_reasoning_task_and_voxelizes_primitives() -> None:
    router = _StubRouter({"primitives": [_box(12, 4, 12, 6, "#33aa55")]})
    vox = await statue_voxels(router, subject="a cat")
    assert router.calls and router.calls[0][0] == "pet.statue"  # the reasoning-bound route
    assert router.calls[0][1] == "a cat"
    assert vox and all(isinstance(v, Voxel) for v in vox)
    assert all(v.c == "#33aa55" for v in vox)  # the box's colour carried through
    assert all(0 <= v.x < STATUE_GRID and 0 <= v.y < STATUE_GRID for v in vox)


async def test_statue_voxels_raises_on_an_empty_model() -> None:
    with pytest.raises(ValueError):
        await statue_voxels(_StubRouter({"primitives": []}), subject="nothing")
    with pytest.raises(ValueError):
        await statue_voxels(_StubRouter("not a dict"), subject="garbage")


def test_voxelize_keeps_only_the_surface_shell() -> None:
    # A solid 6-cube box: the shell (faces) is present, the very inside is hollowed out.
    vox = voxelize([_box(12, 12, 12, 6)])
    cells = {(v.x, v.y, v.z) for v in vox}
    assert (9, 12, 12) in cells  # a face cell is on the shell
    assert (12, 12, 12) not in cells  # the centre is buried → removed
    assert vox  # non-empty


def test_voxelize_cone_tapers_to_a_point() -> None:
    # A y-axis cone: wide at the base, a point at the top → more span low than high.
    cone = {
        "type": "cone",
        "cx": 12,
        "cy": 12,
        "cz": 12,
        "sx": 10,
        "sy": 12,
        "sz": 10,
        "axis": "y",
        "c": "#ffffff",
    }
    vox = voxelize([cone])
    by_y: dict[int, int] = {}
    for v in vox:
        by_y[v.y] = by_y.get(v.y, 0) + 1
    low = min(by_y), max(by_y)
    assert by_y[low[0]] > by_y[low[1]]  # the base layer is wider than the apex layer


def test_clean_primitives_drops_bad_shapes_and_normalises() -> None:
    raw = {
        "primitives": [
            _box(12, 4, 12, 6, "#f80"),  # #rgb shorthand → expands
            {"type": "triangle", "cx": 1, "cy": 1, "cz": 1, "sx": 1, "sy": 1, "sz": 1, "c": "#fff"},
            {"type": "box", "cx": "oops", "cy": 1, "cz": 1, "sx": 1, "sy": 1, "sz": 1, "c": "#fff"},
            "junk",
            {"type": "cylinder", "cx": 5, "cy": 5, "cz": 5, "sx": 3, "sy": 8, "sz": 3, "c": "nope"},
        ]
    }
    out = _clean_primitives(raw)
    assert len(out) == 2  # only the box + cylinder survive
    assert out[0]["c"] == "#ff8800"  # expanded shorthand
    assert out[1]["c"] == "#9aa0b0"  # unusable colour → grey
    assert out[1]["axis"] == "y"  # missing axis defaults to y


def test_clean_primitives_tolerates_junk_input() -> None:
    assert _clean_primitives(None) == []
    assert _clean_primitives({"primitives": "nope"}) == []
    assert _clean_primitives({}) == []


def test_statue_system_prompt_examples_are_valid_and_fit_the_grid() -> None:
    """The two worked examples are authored on a 32-grid and scaled to STATUE_GRID at build time;
    a scaling slip could push a coordinate out of bounds or emit broken JSON, so guard both. The
    scaled examples must also survive the same cleaner the live model output goes through."""
    prompt = _statue_system_prompt()
    assert f"{STATUE_GRID}×{STATUE_GRID}×{STATUE_GRID}" in prompt
    blocks = re.findall(r'\{"primitives":\[.*?\]\}', prompt, re.S)
    assert len(blocks) == 2  # the pig + the monkey
    for block in blocks:
        parsed = json.loads(block)  # valid JSON
        cleaned = _clean_primitives(parsed)
        assert len(cleaned) == len(parsed["primitives"])  # every example primitive is well-formed
        for p in parsed["primitives"]:
            assert all(0 <= p[k] < STATUE_GRID for k in ("cx", "cy", "cz"))  # centres in bounds
            assert all(p[k] >= 1 for k in ("sx", "sy", "sz"))  # non-degenerate sizes
