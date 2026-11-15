"""growbot-drive.js: one command in the front camera's frame, turned into each skill's own
inputs, and the selector's stop, spin, then walk. The frame is the owner's (2026-11-15): the
person is tracked by the front camera, the tall walk travels the way it looks, and the nose walk
backs away from it while it keeps facing them, never turning round."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_DRIVE = Path(__file__).resolve().parents[3] / "growbot" / "patches" / "growbot-drive.js"
_SITE = Path(__file__).resolve().parents[3] / "growbot" / "site" / "growbot-drive.js"


def _node(body: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"const d = await import({json.dumps(_DRIVE.as_uri())});"
        f"console.log(JSON.stringify(await (async () => {{ {body} }})()));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(run.stdout)


def _file(facing: str, speed: str, chans: list[str], low: float = -1, cap: float = 0.03) -> dict:
    spec: dict[str, Any] = {
        "control_hz": 25,
        "segments": [{"channels": ["gyro_z", "cmd_yaw", *chans], "frames": 1}],
        "cmd_yaw": {"meaning": "heading_error", "facing": facing},
    }
    if "cmd_forward" in chans:
        spec["cmd_forward"] = {"range": [low, 1]}
    return {"obs_spec": spec, "training": {"speed": speed, "speed_cap_m_s": cap}}


# The stand-ins, as GrowBot_Training exports them.
LOOK = _file("camera", "still", ["cmd_pitch"])
LIFT_FALL = _file("camera", "cap", [], cap=0.05)
NOSE_WALK = _file("nose", "command", ["cmd_forward"], low=0.0)
MASTER = _file("camera", "command", ["cmd_forward", "cmd_pitch"], cap=0.05)


def _inputs(pj: dict, mode: str, heading: float, speed: float, pitch: float = 0.5) -> dict:
    cmd = {"heading": heading, "speed": speed, "pitch": pitch}
    return _node(
        f"return d.inputs(d.profile({json.dumps(pj)}), {json.dumps(mode)}, {json.dumps(cmd)});"
    )


def test_the_site_serves_the_patch_file() -> None:
    assert _SITE.read_bytes() == _DRIVE.read_bytes()


@pytest.mark.parametrize("heading", [0, 40, -40, 170])
def test_look_turns_toward_the_person_and_stands(heading: float) -> None:
    got = _inputs(LOOK, "look", heading, 0)
    # Positive heading = the person is to the camera's left = turn left; the same for every file.
    assert got == {"fwd": 0, "yaw": pytest.approx(heading / 180), "pitch": 0.5, "ms": 0}


@pytest.mark.parametrize("heading", [0, 20, -20])
def test_the_tall_walk_walks_toward_what_the_camera_sees(heading: float) -> None:
    got = _inputs(LIFT_FALL, "forward", heading, 0.4)
    # One speed only: any forward command is its full 5 cm/s, and it has no camera height.
    assert got == {"fwd": 1, "yaw": pytest.approx(heading / 180), "ms": 0.05}


@pytest.mark.parametrize("heading", [0, 20, -20])
def test_too_close_backs_straight_away_still_facing_the_person(heading: float) -> None:
    """The nose walk travels opposite the camera. Its target is away from the person, which is
    the person's bearing turned half a turn; measured from the nose (also half a turn round)
    that is the same bearing. So the heading passes through unflipped, and the backing speed
    is the nose walk's own forward."""
    got = _inputs(NOSE_WALK, "back", heading, -0.5)
    assert got == {"fwd": 0.5, "yaw": pytest.approx(heading / 180), "ms": pytest.approx(0.015)}


def test_a_master_policy_backs_on_its_own_negative_speed() -> None:
    got = _inputs(MASTER, "back", 10, -0.5)
    assert got["fwd"] == -0.5 and got["yaw"] == pytest.approx(10 / 180)
    assert _inputs(MASTER, "forward", 10, 0.7)["fwd"] == 0.7
    assert _inputs(MASTER, "look", 10, 0.7)["fwd"] == 0


def test_a_heading_wraps_and_a_speed_clamps() -> None:
    assert _inputs(LOOK, "look", 270, 0)["yaw"] == pytest.approx(-0.5)
    assert _inputs(NOSE_WALK, "back", 0, -3)["fwd"] == 1


def test_a_file_not_steered_by_a_heading_is_refused() -> None:
    bad = {"obs_spec": {"cmd_yaw": {"meaning": "turn"}}}
    out = _node(
        f"try {{ d.profile({json.dumps(bad)}); return 'ran'; }} catch (e) {{ return e.message; }}"
    )
    assert "heading" in out


def _modes(cmds: list[tuple[float, float, int]], has: dict[str, bool] | None = None) -> list:
    """The selector's mode after each (heading, speed, ms) command."""
    has = has or {"look": True, "forward": True, "back": True}
    return _node(
        f"const s = new d.Selector({json.dumps(has)}, 0);"
        f"return {json.dumps(cmds)}.map(([h, v, t]) => s.pick({{ heading: h, speed: v }}, t).mode);"
    )


def test_a_sharp_turn_is_stop_spin_then_walk() -> None:
    got = _modes([(90, 1, 0), (60, 1, 500), (20, 1, 1000), (10, 1, 1600)])
    # Facing away: it looks (spins) until the person is within 30 degrees, then walks.
    assert got == ["look", "look", "forward", "forward"]
    # Walking, a turn past 45 degrees stops it at once.
    assert _modes([(0, 1, 0), (50, 1, 100)]) == ["forward", "look"]


def test_speed_zero_face_tracks_and_back_never_turns_round() -> None:
    assert _modes([(30, 0, 0), (-100, 0, 100)]) == ["look", "look"]
    assert _modes([(10, -0.5, 0), (25, -0.5, 100)]) == ["back", "back"]


def test_only_the_speeds_sign_picks_the_nose_walk_never_the_heading() -> None:
    """Owner, 2026-11-15: a target behind at speed 0 or ahead is a spin, never backing; a
    negative speed is the nose walk whatever the heading, steering as it goes."""
    assert _modes([(170, 0, 0), (-150, 1, 100)]) == ["look", "look"]
    assert _modes([(120, -0.5, 0), (-170, -0.5, 100)]) == ["back", "back"]


def test_a_walk_waits_a_moment_after_stopping_and_never_flips_straight_over() -> None:
    got = _modes([(0, 1, 0), (0, 0, 100), (0, 1, 500), (0, 1, 1700), (0, -1, 1800), (0, -1, 3400)])
    assert got == ["forward", "look", "look", "forward", "look", "back"]


def test_a_skill_the_set_lacks_falls_back_to_look() -> None:
    assert _modes([(0, -1, 0)], {"look": True, "forward": True}) == ["look"]


def test_speed_hovering_at_the_threshold_does_not_chatter() -> None:
    got = _modes([(0, 0.2, 0), (0, 0.1, 100), (0, 0.2, 200), (0, 0.1, 300)])
    assert got == ["forward"] * 4
