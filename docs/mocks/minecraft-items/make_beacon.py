"""Draws the No-Spawn Beacon's faces and icon in set A (plan §M16; README.md in this folder).

Set A's language from make_icons.py: tuff in a copper frame, amethyst for Dave's signal.
The top is the recipe seen from above, as the Replicator's is: eight echo shards round a
lit lens where the Power Pack sits. Kept apart from make_icons.py so the beacon's wave and
the Replicator's never edit one file.

    python3 make_beacon.py [preview.png]
"""

import sys

from PIL import Image
from make_icons import HERE, N, PAL, blank, body, box, frame, iso, render, rows

# Echo shard teal, the beacon's one colour beyond set A's.
PALETTE = {**PAL["a"], "h": "#0c4650", "H": "#3fd6d0"}


def shard(c, x0, y0):
    for dy, line in enumerate(("..hH", ".hHh", "hHh.", "Hh..")):
        for dx, ch in enumerate(line):
            if ch != ".":
                c[y0 + dy][x0 + dx] = ch


def lens(c, x0, y0):
    """The emitter: a lit amethyst lens in a dark copper ring, Dave's eye on the area."""
    box(c, x0, y0, x0 + 3, y0 + 3, "d")
    box(c, x0 + 1, y0 + 1, x0 + 2, y0 + 2, "A")
    c[y0 + 1][x0 + 1] = "W"
    c[y0 + 2][x0 + 2] = "a"


def top():
    c = body("a", 41)
    frame(c, "a")
    for gy, y0 in enumerate((1, 6, 11)):
        for gx, x0 in enumerate((1, 6, 11)):
            if gx == gy == 1:
                lens(c, x0, y0)
            else:
                shard(c, x0, y0)
    return rows(c)


def side():
    """An amethyst crystal column in a copper housing, with echo-teal lights either side."""
    c = body("a", 43)
    frame(c, "a")
    box(c, 5, 2, 10, 13, "K")
    box(c, 6, 3, 9, 12, "a")
    box(c, 7, 3, 8, 12, "A")
    for y in (4, 7, 10):
        c[y][7] = "W"
    box(c, 5, 2, 10, 2, "C")
    box(c, 5, 13, 10, 13, "d")
    for y in (4, 7, 10):
        c[y][3] = "H"
        c[y][12] = "h"
    return rows(c)


def bottom():
    c = body("a", 47)
    frame(c, "a")
    box(c, 6, 6, 9, 9, "d")
    return rows(c)


def build():
    faces = {
        "no_spawn_beacon_top": render(top(), PALETTE),
        "no_spawn_beacon_side": render(side(), PALETTE),
        "no_spawn_beacon_bottom": render(bottom(), PALETTE),
    }
    s, t = faces["no_spawn_beacon_side"], faces["no_spawn_beacon_top"]
    out = {"no_spawn_beacon": iso(s, s, t), **faces}
    for name, im in out.items():
        im.save(HERE / "a" / f"{name}.png")
    return out


def preview(out, dest):
    """Each face and the icon at 8x, then the icon at 1x and 2x, on a tuff-grey ground."""
    names = ["no_spawn_beacon", "no_spawn_beacon_top", "no_spawn_beacon_side",
             "no_spawn_beacon_bottom"]
    pad, big = 16, N * 8
    sheet = Image.new("RGBA", (pad + len(names) * (big + pad), big + 3 * pad + N * 2),
                      (48, 48, 52, 255))
    for i, n in enumerate(names):
        sheet.alpha_composite(out[n].resize((big, big), Image.Resampling.NEAREST),
                              (pad + i * (big + pad), pad))
    y = 2 * pad + big
    sheet.alpha_composite(out["no_spawn_beacon"], (pad, y))
    sheet.alpha_composite(out["no_spawn_beacon"].resize((N * 2,) * 2,
                                                        Image.Resampling.NEAREST),
                          (pad + N + 8, y))
    sheet.save(dest)


if __name__ == "__main__":
    assert len(blank()) == N
    o = build()
    if len(sys.argv) > 1:
        preview(o, sys.argv[1])
