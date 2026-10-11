"""Draws the Replicator's working look (plan §M15; README.md in this folder).

Owner request, 2026-10-11: while it makes items the Replicator visibly works. Its front
window and top centre become an 8-frame flipbook, a slow amethyst pulse with the furnace
mouth glowing ember, in set A's palette. Each texture is the frames stacked top to bottom
(16 x 128), as Bedrock's flipbook_textures.json expects. Kept apart from make_icons.py so
this wave and the others never edit one file.

    python3 make_replicator_working.py [preview.png]
"""

import math
import sys

from PIL import Image
from make_icons import HERE, N, PAL, a_front, a_top, render

FRAMES = 8


def mix(c0, c1, t):
    a = [int(c0[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(c1[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b, strict=True))


def level(f):
    """0 at frame 0 rising to 1 mid-cycle and back: one slow breath."""
    return (1 - math.cos(2 * math.pi * f / FRAMES)) / 2


def palette(f):
    t = level(f)
    pal = dict(PAL["a"])
    pal["a"] = mix("#6e4fa8", PAL["a"]["A"], t)  # the window's rim
    pal["A"] = mix(PAL["a"]["a"], "#f0e0ff", t)  # its glow
    pal["W"] = mix(PAL["a"]["A"], "#ffffff", t)  # its core
    pal["k"] = mix("#3a1a08", PAL["a"]["O"], t)  # the furnace mouth: an ember behind bars
    return pal


def strip(grid):
    out = Image.new("RGBA", (N, N * FRAMES))
    for f in range(FRAMES):
        out.paste(render(grid, palette(f)), (0, f * N))
    return out


def build():
    out = {
        "replicator_front_working": strip(a_front(True)),
        "replicator_top_working": strip(a_top(True)),
    }
    for name, im in out.items():
        im.save(HERE / "a" / f"{name}.png")
    return out


def preview(out, dest, scale=8, pad=8):
    """Both strips side by side at 8x, frames left to right."""
    names = list(out)
    w = pad + FRAMES * (N * scale + pad)
    h = pad + len(names) * (N * scale + pad)
    sheet = Image.new("RGBA", (w, h), (40, 42, 46, 255))
    for r, name in enumerate(names):
        for f in range(FRAMES):
            frame = out[name].crop((0, f * N, N, (f + 1) * N))
            big = frame.resize((N * scale, N * scale), Image.NEAREST)
            sheet.alpha_composite(big, (pad + f * (N * scale + pad), pad + r * (N * scale + pad)))
    sheet.save(dest)


if __name__ == "__main__":
    built = build()
    if len(sys.argv) > 1:
        preview(built, sys.argv[1])
