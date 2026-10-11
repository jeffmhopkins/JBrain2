"""Draws the wormhole gate's art in set A's language (MINECRAFT_BEDROCK_PLAN.md §M12): the
Wormhole Seed's icon and the gate's surface, a seamless flipbook strip.

The palette and the drawing helpers are make_icons.py's, so the seed sits beside the
Tricorder and the Power Pack as one people's tech: copper and amethyst, outlined like a
vanilla item, with an Ender Pearl's teal in its window.

    python3 make_wormhole.py [preview.png]
"""

import math
import sys
from pathlib import Path

from PIL import Image

from make_icons import PAL, N, hexrgb, render

HERE = Path(__file__).resolve().parent

# A chorus seed: a lilac pod (popped chorus fruit) under a copper collar and a green
# sprout, with a window onto the fold: the dark between two places, a pearl's teal in it.
SEED = [
    "................",
    "........KK......",
    ".......KVvK.....",
    ".......KvK......",
    ".....KKKKKK.....",
    "....KCCCccdK....",
    "...KAAAAAaapK...",
    "..KAAAWAaaappK..",
    "..KAaSSSSsaapK..",
    "..KAaSmMMSsapK..",
    "..KaaSMWmSsppK..",
    "..KaasSmSSappK..",
    "..KaaasSSsappK..",
    "...KaaaaapppK...",
    "....KaappppK....",
    ".....KKKKKK.....",
]

# The surface, darkest to brightest, then the pearl-teal glints the fold throws.
BANDS = ["S", "s", "p", "a", "A", "W"]
GLINT = ["m", "M"]
FRAMES = 16
# Mostly see-through: what is on the other side is not shown, but the gate's own back
# wall and the sky behind it still read through the shimmer.
ALPHA = 200


def surface_frame(f):
    """One frame. Every wave has a whole number of cycles across the 16 pixels and over
    the loop, so a gate's six blocks tile without seams and the strip loops without a
    jump."""
    t = 2 * math.pi * f / FRAMES
    im = Image.new("RGBA", (N, N))
    pal = PAL["a"]
    for y in range(N):
        for x in range(N):
            u, w = 2 * math.pi * x / N, 2 * math.pi * y / N
            # A wave bent by another: a slow fold rather than a lattice.
            v = math.sin(u + t + 1.2 * math.sin(w + t)) + 0.7 * math.sin(
                w - t + 1.2 * math.sin(u - t)
            )
            band = min(len(BANDS) - 1, max(0, int((v + 1.7) / 3.4 * len(BANDS))))
            ch = BANDS[band]
            glint = math.sin(2 * u + w - 2 * t) * math.sin(u - 2 * w + t)
            if glint > 0.88:
                ch = GLINT[1] if glint > 0.96 else GLINT[0]
            r, g, b, _ = hexrgb(pal[ch])
            im.putpixel((x, y), (r, g, b, ALPHA))
    return im


def surface_strip():
    strip = Image.new("RGBA", (N, N * FRAMES))
    for f in range(FRAMES):
        strip.paste(surface_frame(f), (0, N * f))
    return strip


def preview(seed, strip, dest, scale=8):
    """The seed beside the gate as it stands: a 4x5 ring of diamond blocks (corners
    left out) round a 2x3 surface on frame 0, then frames 0-3 as a 2x2, each pixel 8x8."""
    diamond = Image.new("RGBA", (N, N), hexrgb("#7fe3dc"))
    for i in range(N):
        diamond.putpixel((i, 0), hexrgb("#d9fffb"))
        diamond.putpixel((0, i), hexrgb("#d9fffb"))
        diamond.putpixel((i, N - 1), hexrgb("#3a9e98"))
        diamond.putpixel((N - 1, i), hexrgb("#3a9e98"))
    gate = Image.new("RGBA", (4 * N, 5 * N), (0, 0, 0, 0))
    frame0 = strip.crop((0, 0, N, N))
    for gy in range(5):
        for gx in range(4):
            corner = gx in (0, 3) and gy in (0, 4)
            ring = gx in (0, 3) or gy in (0, 4)
            if ring and not corner:
                gate.paste(diamond, (gx * N, gy * N))
            elif not ring:
                gate.alpha_composite(frame0, (gx * N, gy * N))
    pad = 4
    w = (N + pad + 4 * N + pad + 4 * N) * scale
    sheet = Image.new("RGBA", (w + 2 * pad * scale, (5 * N + 2 * pad) * scale), "#1b1d22")

    def big(im):
        return im.resize((im.width * scale, im.height * scale), Image.Resampling.NEAREST)

    sheet.alpha_composite(big(seed), (pad * scale, pad * scale))
    sheet.alpha_composite(big(gate), ((N + 2 * pad) * scale, pad * scale))
    x0 = (N + 2 * pad + 4 * N + pad) * scale
    for f in range(4):
        at = (x0 + (f % 2) * N * scale, pad * scale + (f // 2) * N * scale)
        sheet.alpha_composite(big(strip.crop((0, N * f, N, N * f + N))), at)
    sheet.save(dest)


def build():
    seed = render(SEED, PAL["a"])
    strip = surface_strip()
    seed.save(HERE / "a" / "wormhole_seed.png")
    strip.save(HERE / "a" / "wormhole.png")
    return seed, strip


if __name__ == "__main__":
    s, st = build()
    if len(sys.argv) > 1:
        preview(s, st, sys.argv[1])
