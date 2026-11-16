"""The app's policy runner (growbot/site/growbot_runner.js) feeds the camera-height
command to a policy that declares cmd_pitch. The full float-for-float parity with the
simulator lives in GrowBot_Training's own CI, which runs this file from main."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_RUNNER = Path(__file__).resolve().parents[3] / "growbot" / "site" / "growbot_runner.js"


def _actions(channels: list[str], cmds: list[dict[str, float]]) -> list[list[float]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    n = len(channels)
    # One linear layer reading only the last channel, so the action is tanh(that input).
    w = [[1.0 if i == n - 1 else 0.0, 0.0] for i in range(n)]
    policy = {
        "obs_size": n,
        "act_size": 2,
        "mean": [0.0] * n,
        "std": [1.0] * n,
        "layers": [{"W": w, "b": [0.0, 0.0], "act": "linear"}],
        "obs_spec": {
            "control_hz": 25,
            "action_to_rad": 1.0,
            "imu_frame": {"up": "+z"},
            "segments": [{"channels": channels, "frames": 1}],
        },
    }
    script = (
        f"const {{ Walker }} = await import({json.dumps(_RUNNER.as_uri())});"
        f"const w = new Walker({json.dumps(policy)}, {{ IMU_SIGN: [1, 1, 1] }});"
        "const g = { x: 0, y: 0, z: 9.81 }, z = { x: 0, y: 0, z: 0 };"
        f"const out = {json.dumps(cmds)}.map((c) => w.step(g, z, c).action);"
        "console.log(JSON.stringify(out));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(run.stdout)


def test_cmd_pitch_reaches_the_policy() -> None:
    out = _actions(["accel_z", "cmd_yaw", "cmd_pitch"], [{"yaw": 0.2, "pitch": -0.5}, {}])
    assert out[0][0] == pytest.approx(-0.462117, abs=1e-5)  # tanh(-0.5)
    assert out[1][0] == pytest.approx(0.761594, abs=1e-5)  # no pitch sent: tall, tanh(1)


def _filtered(af: object, cmds: list[dict[str, float]]) -> list[dict[str, list[float]]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    # The action is tanh(cmd_yaw), so each tick's raw output is known up front.
    policy = {
        "obs_size": 1,
        "act_size": 2,
        "mean": [0.0],
        "std": [1.0],
        "layers": [{"W": [[1.0, 1.0]], "b": [0.0, 0.0], "act": "linear"}],
        "obs_spec": {
            "control_hz": 25,
            "action_to_rad": 1.0,
            "imu_frame": {"up": "+z"},
            "segments": [{"channels": ["cmd_yaw"], "frames": 1}],
            "action_filter": af,
        },
    }
    script = (
        f"const {{ Walker }} = await import({json.dumps(_RUNNER.as_uri())});"
        f"const w = new Walker({json.dumps(policy)}, {{ IMU_SIGN: [1, 1, 1] }});"
        "const g = { x: 0, y: 0, z: 9.81 }, z = { x: 0, y: 0, z: 0 };"
        f"const out = {json.dumps(cmds)}.map((c) => w.step(g, z, c));"
        "console.log(JSON.stringify(out.map((o) => ({ a: o.action, raw: o.raw }))));"
    )
    run = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True
    )
    if run.returncode:
        raise RuntimeError(run.stderr)
    return json.loads(run.stdout)


def test_a_declared_action_filter_low_passes_by_the_speed_command() -> None:
    import math

    af = {"forward": 0.7, "otherwise": 0.5, "meaning": "..."}
    out = _filtered(af, [{"yaw": 0.5}, {"yaw": -0.5}, {"yaw": -0.5, "fwd": 1}])
    t = math.tanh(0.5)
    assert out[0]["a"][0] == pytest.approx(t)  # the first action goes straight through
    second = t + 0.5 * (-t - t)  # standing: alpha "otherwise"
    assert out[1]["a"][0] == pytest.approx(second)
    assert out[1]["raw"][0] == pytest.approx(-t)
    assert out[2]["a"][0] == pytest.approx(second + 0.7 * (-t - second))  # walking: "forward"


def test_no_filter_sends_the_net_output() -> None:
    out = _filtered(None, [{"yaw": 0.5}, {"yaw": -0.5}])
    assert out[1]["a"] == out[1]["raw"]


@pytest.mark.parametrize("af", [{"forward": 0.7}, {"forward": 0, "otherwise": 0.5}, 0.5])
def test_a_malformed_action_filter_is_refused(af: object) -> None:
    with pytest.raises(RuntimeError, match="action_filter"):
        _filtered(af, [{"yaw": 0.1}])
