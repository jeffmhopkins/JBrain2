"""The agent canvas: a retained scene the model edits by id, and its renderer.

Pure of the DB, the LLM and the network by design (docs/plans/AGENT_CANVAS_PLAN.md
W1) — `scene` is data plus a fold, `render` is pixels. The tool handler in
`agent/drawtools.py` owns everything stateful; these two modules own everything hard.
"""

from jbrain.draw.scene import (
    Background,
    Element,
    OpReport,
    Scene,
    SceneError,
    apply_ops,
)

__all__ = [
    "Background",
    "Element",
    "OpReport",
    "Scene",
    "SceneError",
    "apply_ops",
]
