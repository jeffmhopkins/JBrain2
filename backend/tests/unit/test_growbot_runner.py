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
