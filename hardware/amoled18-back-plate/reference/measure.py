"""Re-derive the back plate's defaults from Waveshare's reference files.

Prints the board outline and mounting-hole centres from the 3D model, and the
case corner radius fitted to the dimension drawing. Run from this folder:

    python3 -m venv .venv && .venv/bin/pip install cadquery-ocp pillow
    .venv/bin/python measure.py
"""

import math
import tempfile
import zipfile
from pathlib import Path

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from PIL import Image

HERE = Path(__file__).parent

# The dimension drawing labels the case 45.20 mm tall; the back view spans this
# many pixels, which sets the drawing's scale.
DRAWING_PX_PER_MM = 338 / 45.2


def bbox(shape):
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b, True)
    lo, hi = b.CornerMin(), b.CornerMax()
    return lo.X(), hi.X(), lo.Y(), hi.Y(), lo.Z(), hi.Z()


def board_and_holes():
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(HERE / "waveshare-3d-model.zip").extractall(tmp)
        reader = STEPControl_Reader()
        reader.ReadFile(str(next(Path(tmp).glob("*.stp"))))
        reader.TransferRoots()
        model = reader.OneShape()

    # The PCB is the only large solid about 1.2 mm thick.
    pcb = None
    exp = TopExp_Explorer(model, TopAbs_SOLID)
    while exp.More():
        x0, x1, y0, y1, z0, z1 = bbox(exp.Current())
        if x1 - x0 > 30 and 1.0 < z1 - z0 < 1.5:
            pcb = exp.Current()
        exp.Next()
    x0, x1, y0, y1, z0, z1 = bbox(pcb)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    print(f"PCB: {x1 - x0:.2f} x {y1 - y0:.2f} x {z1 - z0:.2f} mm")

    # Mounting holes are faceted rings in the corners; average each ring's
    # vertices on one face to get its centre.
    rings = {}
    exp = TopExp_Explorer(pcb, TopAbs_VERTEX)
    while exp.More():
        p = BRep_Tool.Pnt_s(TopoDS.Vertex(exp.Current()))
        x, y = p.X() - cx, p.Y() - cy
        if abs(p.Z() - z0) < 0.05 and 10.5 < abs(x) < 13.5 and 16.5 < abs(y) < 19.5:
            rings.setdefault((x > 0, y > 0), set()).add((round(x, 3), round(y, 3)))
        exp.Next()
    for key, pts in sorted(rings.items()):
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        print(f"  hole centre: ({mx:+.2f}, {my:+.2f}) mm from board centre")


def corner_radius():
    im = Image.open(HERE / "waveshare-dimensions.webp").convert("L")
    # Bottom-right corner of the back view: the only corner clear of both the
    # side buttons and the red dimension arrows.
    pts = []
    for y in range(380, 445):
        xs = [x for x in range(745, 900) if im.getpixel((x, y)) < 70]
        if xs:
            pts.append((max(xs), y))
    ex = max(x for x, _ in pts)
    ey = max(y for _, y in pts)
    best = None
    for r in (i / 2 for i in range(4, 200)):
        cx, cy = ex - r, ey - r
        inside = [(x, y) for x, y in pts if x > cx and y > cy]
        if len(inside) > 5:
            err = sum((math.hypot(x - cx, y - cy) - r) ** 2 for x, y in inside) / len(
                inside
            )
            if best is None or err < best[0]:
                best = (err, r)
    print(
        f"Case corner radius: {best[1] / DRAWING_PX_PER_MM:.1f} mm (fit error {best[0]:.2f} px²)"
    )


if __name__ == "__main__":
    board_and_holes()
    corner_radius()
