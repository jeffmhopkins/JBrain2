"""The structure index (plan §M8): where a sweep asks `locate` from, when a saved sweep
stops answering for its world, and that a sweep keeps each structure once.

The Rig's side — the background thread, the console, the saved file answering with the
server stopped — is in test_minecraft_sidecar.py.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name: str):
    mc_dir = str(DEPLOY / "minecraft")
    if mc_dir not in sys.path:
        sys.path.insert(0, mc_dir)
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"minecraft/{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


structure_sweep = _load("structure_sweep")
KINDS = {"overworld": ("village", "temple"), "nether": ("fortress",)}
EXPLORED = {"min_x": -100, "max_x": 300, "min_z": -50, "max_z": 50, "chunks": 40}


def test_the_axis_is_evenly_spaced_and_never_further_apart_than_a_step() -> None:
    assert structure_sweep.axis(0, 0) == [0]
    got = structure_sweep.axis(-1024, 1100)
    assert got[0] == -1024 and got[-1] == 1100
    assert max(b - a for a, b in itertools.pairwise(got)) <= structure_sweep.STEP


def test_the_swept_area_is_the_explored_one_plus_a_margin_capped_at_the_reach() -> None:
    rect = structure_sweep.area(EXPLORED, (0, 0))
    assert rect == {"min_x": -1124, "max_x": 1324, "min_z": -1074, "max_z": 1074}
    nobody = structure_sweep.area(None, (32, -8))
    assert nobody["min_x"] == 32 - structure_sweep.MARGIN
    assert nobody["max_z"] == -8 + structure_sweep.MARGIN
    wide = {"min_x": -50_000, "max_x": 50_000, "min_z": 0, "max_z": 0}
    huge = structure_sweep.area(wide, (0, 0))
    assert huge["max_x"] - huge["min_x"] == 2 * structure_sweep.REACH


def test_spawn_is_asked_first_then_outward_from_the_middle() -> None:
    rect = {"min_x": -1024, "max_x": 1024, "min_z": -1024, "max_z": 1024}
    pts = structure_sweep.points(rect, (7, 9))
    assert pts[0] == (7, 9) and pts[1] == (0, 0)
    assert len(pts) == len(set(pts)) == 26


def test_a_saved_sweep_goes_stale_on_a_new_seed_new_kinds_or_new_ground() -> None:
    saved = {
        "seed": "42",
        "kinds": KINDS,
        "explored": {"overworld": EXPLORED, "nether": None},
    }
    extents = {"overworld": EXPLORED, "nether": None}
    stale = structure_sweep.stale_reason
    assert stale(None, "42", KINDS, extents) == "never searched"
    assert stale(saved, "42", KINDS, extents) is None
    assert stale(saved, "43", KINDS, extents) == "the world changed"
    more = {**KINDS, "overworld": ("village", "temple", "trial_chambers")}
    assert stale(saved, "42", more, extents) == "the server knows new structure kinds"
    nudged = {**EXPLORED, "max_x": EXPLORED["max_x"] + structure_sweep.SLACK}
    assert stale(saved, "42", KINDS, {**extents, "overworld": nudged}) is None
    grown = {**EXPLORED, "max_x": EXPLORED["max_x"] + structure_sweep.SLACK + 16}
    assert (
        stale(saved, "42", KINDS, {**extents, "overworld": grown}) == "explored further"
    )
    trip = {"min_x": 0, "max_x": 15, "min_z": 0, "max_z": 15}
    assert stale(saved, "42", KINDS, {**extents, "nether": trip}) == "explored further"


def test_a_sweep_keeps_each_structure_once_and_reports_progress() -> None:
    plan = {"overworld": [(0, 0), (600, 0)], "nether": [(0, 0)]}
    seen: list[int] = []

    def locate(dim: str, x: int, z: int, kind: str) -> tuple[int, int] | None:
        if kind == "village":
            return (x + 10, 5)
        if kind == "fortress":
            return (-656, -224)
        return None

    found = structure_sweep.sweep(locate, plan, KINDS, progress=seen.append)
    assert seen == [1, 2, 3, 4, 5] and structure_sweep.total(plan, KINDS) == 5
    assert found["overworld"] == [
        {"structure": "village", "x": 10, "z": 5},
        {"structure": "village", "x": 610, "z": 5},
    ]
    again = structure_sweep.sweep(locate, {"nether": [(0, 0), (900, 900)]}, KINDS)
    assert again["nether"] == [{"structure": "fortress", "x": -656, "z": -224}]


def test_a_sweep_stops_when_its_world_goes_away() -> None:
    calls: list[str] = []

    def locate(dim: str, x: int, z: int, kind: str) -> None:
        calls.append(kind)

    with pytest.raises(InterruptedError):
        structure_sweep.sweep(
            locate, {"overworld": [(0, 0)]}, KINDS, keep_going=lambda: not calls
        )
    assert calls == ["village"]


def test_a_saved_sweep_round_trips_and_an_unknown_format_reads_as_none(
    tmp_path: Path,
) -> None:
    body = structure_sweep.save(
        tmp_path,
        "world",
        seed="42",
        kinds=KINDS,
        explored={"overworld": EXPLORED, "nether": None},
        found={"overworld": [{"structure": "village", "x": 1, "z": 2}], "nether": []},
        at=1.0,
    )
    assert structure_sweep.load(tmp_path, "world") == body
    assert not list(tmp_path.glob("*.tmp"))
    assert structure_sweep.load(tmp_path, "slot2") is None
    (tmp_path / "old.json").write_text(json.dumps({"format": 0}))
    assert structure_sweep.load(tmp_path, "old") is None
    (tmp_path / "torn.json").write_text("{")
    assert structure_sweep.load(tmp_path, "torn") is None
