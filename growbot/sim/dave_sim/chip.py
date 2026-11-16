"""The relay and the robot's chip, as the page's WebSockets meet them in the simulator.

The page dials `/d/<code>` for the legs and `/d/<code>-agent` for the box's side channel
(serve.py). Here the legs' socket is answered by what `growbot/firmware/relay_chip.py` does
with each frame: the walk lane writes the servos at once, the gesture lane plays keyframes
through the firmware's own `ActEngine`, stop goes limp, and the walk dead-man goes limp on
500 ms of pose silence. The servos are the sim body's (GrowBot_Training simbody.py), through
its wiring, so a crossed pin moonwalks here as on the bench.
"""

from __future__ import annotations

import importlib.util
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"
DEADMAN_MS = 500  # relay_chip.py's walk dead-man
ROUTINES = {
    "wiggle": [
        {"l": 60, "r": 120, "ms": 400},
        {"l": 120, "r": 60, "ms": 400},
        {"l": 60, "r": 120, "ms": 400},
        {"l": 120, "r": 60, "ms": 400},
        {"l": 90, "r": 90, "ms": 300},
    ]
}


def _act_engine() -> type:
    spec = importlib.util.spec_from_file_location("act_engine", FIRMWARE / "act_engine.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ActEngine


class Servos(Protocol):
    def servos(self, l_deg: float, r_deg: float) -> None: ...
    def release(self) -> None: ...


def _ms() -> int:
    return int(time.monotonic() * 1000)


class Chip:
    """One robot's chip. `frame()` takes each text frame the page sends on the legs'
    socket and returns the replies; `tick()` runs the chip's own loop (gesture glides and
    the dead-man) and is called every control tick."""

    def __init__(self, body: Servos, clock: Callable[[], int] = _ms) -> None:
        self.body = body
        self.clock = clock
        self.engine = _act_engine()(self._write, self._release, clock, lambda a, b: a - b)
        self.walk_on = False
        self.last_pose = 0
        # Every frame the page sent, with when: the run's record of what Dave told his legs.
        self.log: list[dict[str, Any]] = []
        # The last write either lane made, in wire degrees (None: limp).
        self.pose: tuple[float, float] | None = None

    def _write(self, l_deg: float, r_deg: float) -> None:
        self.pose = (l_deg, r_deg)
        self.body.servos(l_deg, r_deg)

    def _release(self) -> None:
        self.pose = None
        self.body.release()

    def hello(self) -> list[dict[str, Any]]:
        """What the relay tells a page that attaches while the chip is online."""
        return [{"t": "status", "awake": 1, "boot": {"reset": "power", "up_ms": 1000, "fw": "sim"}}]

    def frame(self, text: str) -> list[dict[str, Any]]:
        try:
            m = json.loads(text)
        except ValueError:
            return []
        if not isinstance(m, dict):
            return []
        t = m.get("t")
        now = self.clock()
        self.log.append({"ms": now, **m})
        if t == "attach":
            return self.hello()
        if t == "pose":
            self.engine.clear()
            try:
                ls, rs = str(m.get("lr", "90,90")).split(",")
                self._write(max(0.0, min(180.0, float(ls))), max(0.0, min(180.0, float(rs))))
            except ValueError:
                pass
            self.walk_on, self.last_pose = True, now
            return []
        if t in ("act", "routine"):
            steps = m.get("steps", []) if t == "act" else ROUTINES.get(m.get("name", ""), [])
            ok, q = self.engine.enqueue(
                steps, m.get("mode", "replace") if t == "act" else "replace"
            )
            self.walk_on = False
            return [
                {"t": "ack", "rid": m.get("rid"), "ok": 1 if ok else 0, "queued_ms": q if ok else 0}
            ]
        if t == "stop":
            self.engine.clear()
            self._release()
            self.walk_on = False
            return [{"t": "ack", "rid": m.get("rid"), "ok": 1, "queued_ms": 0}]
        return []

    def tick(self) -> None:
        self.engine.tick()
        if self.walk_on and not self.engine.active and self.clock() - self.last_pose > DEADMAN_MS:
            self._release()
            self.walk_on = False
