# Recovers 4x4 poses from marker STLs (parts.scad's marker(): a dot at the origin and dots
# 1, 2 and 3 mm along x, y and z), so Blender can place the display exactly where OpenSCAD's
# own transforms put the seam. Usage: python marker.py <out.json> <marker.stl>...
import json
import sys
from pathlib import Path

import numpy as np
import trimesh

poses = {}
for path in sys.argv[2:]:
    dots = [p.vertices.mean(axis=0) for p in trimesh.load(path).split(only_watertight=False)]
    origin = min(dots, key=lambda c: sum(np.linalg.norm(c - d) for d in dots))
    m = np.eye(4)
    m[:3, 3] = origin
    for d in dots:
        length = np.linalg.norm(d - origin)
        if length > 0.5:
            m[:3, int(round(length)) - 1] = (d - origin) / length
    poses[Path(path).stem] = m.round(6).tolist()
Path(sys.argv[1]).write_text(json.dumps(poses))
