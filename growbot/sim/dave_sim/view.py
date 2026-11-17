"""What the robot's front camera has in view in the house, from the sim's ground truth: each
piece of furniture and the doorway, where it sits across the picture (0 the left edge, 1 the
right, as Dave's `map` field takes it) and how far it is. The stand-in brain (pilot.py) reads
this in place of eyes, and a test checks the page's bearing maths against it.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from dave_sim.run import Sim

# A thing is "seen" only this close: a phone's picture of a far plant is a few pixels.
SEE_M = 3.5
# Where on a piece of furniture the camera is taken to look: about seat height.
LOOK_Z = 0.3


def project(sim: Sim, x: float, y: float, z: float = LOOK_Z) -> dict[str, float] | None:
    """Where (x, y, z) falls in the front camera's picture, or None when it is behind it or
    off the picture's edges."""
    sb = sim.sb
    m, d = sim.body.model, sim.body.data
    cam = m.camera("front").id
    pos = d.cam_xpos[cam]
    rot = d.cam_xmat[cam].reshape(3, 3)
    v = rot.T @ (np.array([x, y, z]) - pos)
    if v[2] >= 0:
        return None
    f = 0.5 * sb.CAM_H / math.tan(math.radians(sb.CAM_VFOV_DEG) / 2)
    u = 0.5 + f * v[0] / -v[2] / sb.CAM_W
    w = 0.5 - f * v[1] / -v[2] / sb.CAM_H
    if not (0.0 <= u <= 1.0 and 0.0 <= w <= 1.0):
        return None
    return {"x": round(float(u), 3), "y": round(float(w), 3), "m": float(np.linalg.norm(v))}


def visible(sim: Sim) -> list[dict[str, Any]]:
    """The furniture in view within SEE_M, nearest first, and the doorway as "door". A wall
    between him and a thing hides it (the rooms' dividing wall, the one wall inside)."""
    h = sim.sb.house
    s = sim.state
    here = h.room_at(s["x"], s["y"])
    out: list[dict[str, Any]] = []
    for it in h.ITEMS:
        if it.name in ("rug",) or h.room_at(it.x, it.y) != here:
            continue
        p = project(sim, it.x, it.y)
        if p is None or p["m"] > SEE_M:
            continue
        out.append({"name": it.name, **p})
    door = project(sim, h.DIVIDE_X, (h.DOOR[0] + h.DOOR[1]) / 2, 0.8)
    if door is not None and door["m"] <= SEE_M:
        other = next(n for n in h.ROOMS if n != here) if here else ""
        out.append({"name": "door", "to": other, **door})
    out.sort(key=lambda e: e["m"])
    return out
