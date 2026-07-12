"""The JPet field "build a statue of X" voxel generator (jbrain.jpet.brain.statue_voxels).

The LLM sculpts a subject as a sparse 24³ voxel model; this proves the host-side coercion:
out-of-grid / malformed cells are dropped, colours are normalised, cells de-duplicate, and an
empty model raises so the caller can tell the wall it couldn't imagine that one. The model is
faked — no network — via a stub router that returns a preset parsed object.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from jbrain.jpet.brain import STATUE_GRID, _clean_voxels, statue_voxels


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


async def test_statue_voxels_runs_the_high_reasoning_task_and_returns_clean_cells() -> None:
    router = _StubRouter({"voxels": [{"x": 1, "y": 0, "z": 2, "c": "#ff8800"}]})
    vox = await statue_voxels(router, subject="a cat")
    assert router.calls and router.calls[0][0] == "pet.statue"  # the reasoning-bound route
    assert router.calls[0][1] == "a cat"
    assert len(vox) == 1 and vox[0].c == "#ff8800" and (vox[0].x, vox[0].y, vox[0].z) == (1, 0, 2)


async def test_statue_voxels_raises_on_an_empty_model() -> None:
    with pytest.raises(ValueError):
        await statue_voxels(_StubRouter({"voxels": []}), subject="nothing")
    with pytest.raises(ValueError):
        await statue_voxels(_StubRouter("not a dict"), subject="garbage")


def test_clean_voxels_drops_bad_cells_dedupes_and_normalises_colour() -> None:
    g = STATUE_GRID
    raw = {
        "voxels": [
            {"x": 0, "y": 0, "z": 0, "c": "#f80"},  # #rgb shorthand → expands
            {"x": 0, "y": 0, "z": 0, "c": "#000000"},  # duplicate cell → dropped
            {"x": g, "y": 0, "z": 0, "c": "#fff"},  # x out of range → dropped
            {"x": 5, "y": 5, "z": 5, "c": "not-a-colour"},  # bad colour → grey fallback
            {"x": 2, "y": "oops", "z": 1, "c": "#fff"},  # non-int coord → dropped
            "junk",  # non-dict → dropped
        ]
    }
    out = _clean_voxels(raw)
    assert len(out) == 2
    assert out[0].c == "#ff8800"  # #f80 expanded
    assert out[1].c == "#9aa0b0"  # unusable colour → grey
    assert all(0 <= v.x < g and 0 <= v.y < g and 0 <= v.z < g for v in out)


def test_clean_voxels_tolerates_junk_input() -> None:
    assert _clean_voxels(None) == []
    assert _clean_voxels({"voxels": "nope"}) == []
    assert _clean_voxels({}) == []
