"""growbot-follow.js: a face box in the front camera becomes a bearing and a distance, one person
is kept through a crossing, and the follower turns them into growbot-drive.js's command. The
owner's rules (2026-11-15): keep the distance they were at when following began, "get closer" and
"back up" move it, keep the person already tracked, and never follow a stranger."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3] / "growbot"
_FOLLOW = _ROOT / "patches" / "growbot-follow.js"
_SITE = _ROOT / "site" / "growbot-follow.js"


def _node(body: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"const f = await import({json.dumps(_FOLLOW.as_uri())});"
        "const box = (x, w) => ({ x: x - w / 2, y: 0.4 - w / 2, width: w, height: w });"
        f"console.log(JSON.stringify(await (async () => {{ {body} }})()));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(run.stdout)


def test_the_site_serves_the_patch_file() -> None:
    assert _SITE.read_bytes() == _FOLLOW.read_bytes()


def test_a_face_right_of_centre_is_to_the_robots_left() -> None:
    got = _node(
        "return [f.reading(box(0.8, 0.1)), f.reading(box(0.2, 0.1)), f.reading(box(0.5, 0.1))];"
    )
    assert got[0]["bearing"] == pytest.approx(19.1, abs=0.1)
    assert got[1]["bearing"] == pytest.approx(-19.1, abs=0.1)
    assert got[2]["bearing"] == pytest.approx(0, abs=1e-9)
    # A flipped phone reads the other way round.
    flipped = _node("return f.reading(box(0.8, 0.1), { ...f.CAMERA, flipX: true }).bearing;")
    assert flipped == pytest.approx(-19.1, abs=0.1)


def test_a_smaller_face_is_further_and_the_1m_calibration_corrects_it() -> None:
    near, far = _node("return [f.reading(box(0.5, 0.2)).dist, f.reading(box(0.5, 0.1)).dist];")
    assert far == pytest.approx(2 * near)
    k = _node("return f.calibrate(f.reading(box(0.5, 0.2)).dist, 1);")
    fixed = _node(f"return f.reading(box(0.5, 0.2), {{ ...f.CAMERA, k: {k} }}).dist;")
    assert fixed == pytest.approx(1)
    assert _node("return [f.calibrate(0, 1), f.calibrate(1, 0)];") == [1, 1]


def test_the_track_keeps_its_person_when_another_walks_past() -> None:
    """Two people: the tracked one drifts left while a stranger crosses from the right. The track
    keeps the one it had, by where they should be, not the nearer or more central face."""
    got = _node(
        "const t = new f.Track();"
        "t.lock([f.reading(box(0.5, 0.15))], 0, 0);"
        "const seen = [];"
        "for (let i = 1; i <= 10; i++) {"
        "  const me = f.reading(box(0.5 + 0.01 * i, 0.15));"
        "  const them = f.reading(box(0.9 - 0.04 * i, 0.2));"
        "  t.update([them, me], 0, i * 100); seen.push(t.p.az);"
        "}"
        "return seen;"
    )
    # Always the slow drift to the left (positive), never a jump to the stranger.
    assert all(a > 0 for a in got) and got == sorted(got) and got[-1] < 8


def test_a_stranger_far_from_the_prediction_is_never_taken() -> None:
    got = _node(
        "const t = new f.Track();"
        "t.lock([f.reading(box(0.5, 0.15))], 0, 0);"
        "return [t.update([f.reading(box(0.1, 0.15))], 0, 100), t.age(100)];"
    )
    assert got == [False, 100]


def test_the_track_is_kept_in_the_walks_frame_as_the_robot_turns() -> None:
    """The gyro carries the heading between frames: the robot turning 10 degrees left moves the
    face 10 degrees right in the frame, and the person hasn't moved at all."""
    got = _node(
        "const t = new f.Track();"
        "const r0 = f.reading(box(0.7, 0.15));"
        "t.lock([r0], 0, 0);"
        "const turned = 10, rad = (r0.bearing - turned) * Math.PI / 180;"
        "const x = 0.5 + Math.tan(rad) / Math.tan(Math.PI / 6) / 2;"
        "t.update([f.reading(box(x, 0.15))], turned, 100);"
        "return [r0.bearing, t.p.az];"
    )
    assert got[1] == pytest.approx(got[0], abs=1e-6)


def _commands(dists: list[float], set_to: float = 1.0, stay: bool = False) -> list[dict]:
    return _node(
        "const t = new f.Track(), fo = new f.Follower();"
        f"fo.start({set_to}); fo.stay = {json.dumps(stay)};"
        f"return {json.dumps(dists)}.map((d, i) => {{"
        "  t.p = { az: 0, dist: d, size: 0.1, vaz: 0, vdist: 0, t: i * 100, up: 0 }; t.seq++;"
        "  return fo.command(t, 0, i * 100);"
        "});"
    )


def test_the_follower_holds_its_distance_with_a_dead_band() -> None:
    got = _commands([1.0, 1.1, 0.9, 1.5, 0.5, 3.0, 0.1])
    assert [c["speed"] for c in got[:3]] == [0, 0, 0]
    assert got[3]["speed"] == pytest.approx(1.5 * (0.5 - 0.15)) and got[3]["state"] == "follow"
    assert got[4]["speed"] == pytest.approx(-1.5 * (0.5 - 0.15))
    assert got[5]["speed"] == 1 and got[6]["speed"] == -1


def test_stay_turns_to_watch_but_never_walks() -> None:
    got = _commands([2.0], stay=True)
    assert got[0]["speed"] == 0 and got[0]["state"] == "hold"


def test_a_stale_reading_stops_and_a_lost_person_is_turned_toward() -> None:
    got = _node(
        "const t = new f.Track(), fo = new f.Follower();"
        "t.lock([f.reading(box(0.8, 0.05))], 0, 0); fo.start(1);"
        "return [fo.command(t, 0, 100), fo.command(t, 0, 600), fo.command(t, 30, 3500)];"
    )
    fresh, stale, lost = got
    assert fresh["speed"] > 0 and stale["speed"] == 0 and stale["state"] == "hold"
    # Lost: it stands and turns toward where they were last seen, from where it now faces.
    assert lost["state"] == "lost" and lost["speed"] == 0
    assert lost["heading"] == pytest.approx(fresh["heading"] - 30)


def test_closer_and_further_move_the_distance_within_its_limits() -> None:
    got = _node(
        "const fo = new f.Follower(); fo.start(1);"
        "const a = [fo.closer(), fo.further(), fo.further()];"
        "for (let i = 0; i < 20; i++) fo.further();"
        "a.push(fo.set); for (let i = 0; i < 20; i++) fo.closer(); a.push(fo.set);"
        "fo.start(9); a.push(fo.set); return a;"
    )
    assert got == [0.75, 1, 1.25, 3, 0.4, 3]


def test_the_camera_height_creeps_once_per_reading_not_per_tick() -> None:
    got = _node(
        "const t = new f.Track(), fo = new f.Follower(); fo.pitch = 0;"
        "t.lock([f.reading(box(0.5, 0.15))], 0, 0); t.p.up = 10;"
        "fo.command(t, 0, 10); fo.command(t, 0, 50); return fo.command(t, 0, 90).pitch;"
    )
    assert got == pytest.approx(0.2)
