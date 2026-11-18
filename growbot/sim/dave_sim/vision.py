"""Dave's depth sense in the simulator (growbot/patches/growbot-vision.js): the maps a phone's
depth and segmentation models would make of his camera's picture, rendered from the sim's
ground truth instead. MuJoCo draws each pixel's depth and which geom it shows; the geom's name
gives its label in the page's vocabulary. The page reduces the maps with the same code a phone
would, so a run tests that code and what Dave does with it, not the models.

`noisy` makes the maps look like a phone model's: a whole-frame scale that wanders (a single
camera's metric depth is a guess at scale), a few percent of jitter per pixel, holes, and
labels lost to "unknown". `truth` sends them as rendered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# numpy and MuJoCo come with the sim's own venv; the backend's tests load this file for its
# labels without them.
if TYPE_CHECKING:
    import numpy as np

# The page's VOCAB, in order: a label is its index.
VOCAB = (
    "unknown", "floor", "wall", "ceiling", "door", "person", "animal", "sofa", "chair", "table",
    "shelf", "lamp", "plant", "piano", "fridge", "counter", "bed", "bench", "cabinet", "screen",
    "self",
)  # fmt: skip
_IDX = {n: i for i, n in enumerate(VOCAB)}

# Geom name (its number suffix dropped) to label. The house's own walls are "house_*"; the
# robot's body is "self", which the page leaves out.
_BY_PREFIX = (
    ("floor", "floor"),
    ("rug", "floor"),
    ("house_ceiling", "ceiling"),
    ("house_", "wall"),
    # The bare room's own.
    ("ceiling", "ceiling"),
    ("wall_", "wall"),
    ("piano", "piano"),
    ("bench", "bench"),
    ("couch", "sofa"),
    ("bookshelf", "shelf"),
    ("plant", "plant"),
    ("lamp", "lamp"),
    ("fridge", "fridge"),
    ("counter", "counter"),
    ("kettle", "counter"),
    ("table", "table"),
    ("fruit_bowl", "table"),
    ("cat_", "animal"),
    ("torso_", "self"),
    ("lower_leg", "self"),
    ("phone", "self"),
    ("face", "person"),
    ("head", "person"),
    ("torso", "person"),
)

# The maps' size: a phone model's output is small too, and the page walks every pixel.
W, H = 48, 64


def label_of(name: str) -> str:
    for prefix, label in _BY_PREFIX:
        if name == prefix or name.startswith(prefix):
            return label
    return "unknown"


def geom_labels(model: Any) -> np.ndarray:
    """Each geom's label index, by geom id."""
    import numpy as np

    out = np.zeros(model.ngeom, dtype=np.uint8)
    for i in range(model.ngeom):
        out[i] = _IDX[label_of(model.geom(i).name or "")]
    return out


def camera_pose(model: Any, data: Any) -> tuple[float, float]:
    """The front camera's pitch (degrees above level) and height off the floor (metres)."""
    cam = model.camera("front").id
    fwd = -data.cam_xmat[cam].reshape(3, 3)[:, 2]
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, float(fwd[2])))))
    return pitch, float(data.cam_xpos[cam][2])


def _rng(seed: int) -> Any:
    import numpy as np

    return np.random.default_rng(seed)


@dataclass
class Noise:
    """A phone model's errors, roughly: Depth Anything's metric-indoor scale is good to about
    a tenth indoors and wanders between frames; its edges smear and its labels miss."""

    scale_sd: float = 0.08
    scale_keep: float = 0.8
    pixel_sd: float = 0.03
    holes: float = 0.02
    unknown: float = 0.05
    rng: Any = field(default_factory=lambda: _rng(0))
    _log_scale: float = 0.0

    def apply(self, depth: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        r = self.rng
        k = self.scale_keep
        self._log_scale = k * self._log_scale + math.sqrt(1 - k * k) * r.normal(0, self.scale_sd)
        d = depth * math.exp(self._log_scale) * (1 + r.normal(0, self.pixel_sd, depth.shape))
        d[r.random(depth.shape) < self.holes] = 0.0
        lab = labels.copy()
        # His own body stays his: a model that lost its label would still see it a few cm off,
        # which the page leaves out by distance anyway.
        lab[(r.random(labels.shape) < self.unknown) & (labels != _IDX["self"])] = _IDX["unknown"]
        return d, lab


class Maps:
    """Renders the maps for one camera pose. Not thread-safe: one per drawing thread."""

    def __init__(self, model: Any, noise: Noise | None) -> None:
        import mujoco

        self.model = model
        self.noise = noise
        self.labels = geom_labels(model)
        self.r = mujoco.Renderer(model, H, W)
        self.vfov = float(model.cam_fovy[model.camera("front").id])

    def render(self, data: Any) -> dict[str, Any]:
        """The maps for data's pose, whose kinematics the caller has run."""
        import mujoco
        import numpy as np

        mujoco.mj_camlight(self.model, data)
        r = self.r
        r.enable_depth_rendering()
        r.update_scene(data, camera="front")
        depth = r.render().astype(np.float64)
        r.disable_depth_rendering()
        r.enable_segmentation_rendering()
        r.update_scene(data, camera="front")
        seg = r.render()
        r.disable_segmentation_rendering()
        ids, kinds = seg[..., 0], seg[..., 1]
        geom = (kinds == mujoco.mjtObj.mjOBJ_GEOM) & (ids >= 0)
        lab = np.zeros(ids.shape, dtype=np.uint8)
        lab[geom] = self.labels[ids[geom]]
        depth[~geom] = 0.0
        if self.noise is not None:
            depth, lab = self.noise.apply(depth, lab)
        pitch, cam_h = camera_pose(self.model, data)
        return {
            "w": W,
            "h": H,
            "depth_mm": [int(v) for v in np.clip(depth * 1000, 0, 65535).astype(np.uint16).ravel()],
            "labels": [int(v) for v in lab.ravel()],
            "classes": VOCAB,
            "pitch": round(pitch, 2),
            "camH": round(cam_h, 3),
            "vfov": self.vfov,
            "src": "sim-noisy" if self.noise is not None else "sim-truth",
        }
