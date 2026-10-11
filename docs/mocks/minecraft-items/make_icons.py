"""Draws the GUI-gate icons for the three Old Maker items (README.md in this folder).

Every PNG here is 16x16 RGBA, drawn pixel by pixel from the grids and shape rules below,
so a revision round edits a grid and re-runs this rather than touching the PNGs by hand.
The vanilla stand-ins are our own rough approximations for contrast, not Mojang textures.

    python3 make_icons.py [contact_sheet_dir]
"""

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
N = 16


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) + (255,)


def render(grid, pal):
    assert len(grid) == N, len(grid)
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    for y, row in enumerate(grid):
        assert len(row) == N, (y, row, len(row))
        for x, ch in enumerate(row):
            if ch != ".":
                im.putpixel((x, y), hexrgb(pal[ch]))
    return im


def blank():
    return [["."] * N for _ in range(N)]


def rows(cells):
    return ["".join(r) for r in cells]


def outline(cells, ink="K"):
    """Ring the shape's edge with the outline ink, as vanilla items do."""
    inside = [[c != "." for c in r] for r in cells]
    out = [r[:] for r in cells]
    for y in range(N):
        for x in range(N):
            if not inside[y][x]:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if not (0 <= nx < N and 0 <= ny < N) or not inside[ny][nx]:
                    out[y][x] = ink
                    break
    return out


# ---- shared colours -------------------------------------------------------------------
# The Eye-of-Ender green ties every Power Pack to its recipe; each variant's housing is
# what separates it from a loose eye or a pearl at hotbar size.
EYE = {"e": "#2f9a6a", "E": "#8fe8b0", "g": "#16583c", "k": "#0a1712", "w": "#e9fff2"}

# ---- variant palettes -----------------------------------------------------------------
PAL = {
    "a": {  # salvaged copper, verdigris and amethyst over tuff
        ".": None,
        "K": "#2b1a14", "C": "#e59a6e", "c": "#b8684a", "d": "#7c3d2b",
        "v": "#4fae8f", "V": "#8fdcbc",
        "A": "#d7b0ff", "a": "#9a72d6", "p": "#5b3d93",
        "S": "#22183a", "s": "#3a2a5c", "W": "#ead6ff",
        "t": "#6e7068", "T": "#83857c", "u": "#5a5c54",
        "i": "#e4e4e4", "I": "#ffffff", "j": "#a9a9a9",
        "x": "#141010",
        **EYE,
    },
    "b": {  # deep-dark: deepslate, sculk glow and echo shard
        ".": None,
        "K": "#05080b", "D": "#5b606c", "d": "#3e424c", "x": "#26282f",
        "q": "#0b3f4a", "T": "#3ff0e8", "t": "#139a9a",
        "h": "#1c4a55", "H": "#3c8ea2", "W": "#e2ffff",
        "i": "#e4e4e4", "I": "#ffffff", "j": "#a9a9a9",
        **EYE,
    },
    "c": {  # ship's quartz, gold contacts and redstone
        ".": None,
        "K": "#2a2016", "Q": "#f4efe6", "q": "#d6cdbd", "n": "#a99d88",
        "Y": "#ffe27a", "y": "#d9a92e", "o": "#8a5f12",
        "R": "#ff3b2e", "r": "#a3150e", "s": "#24120f",
        "i": "#e4e4e4", "I": "#ffffff", "j": "#a9a9a9",
        **EYE,
    },
}

# ---- Tricorders -----------------------------------------------------------------------
TRICORDER = {
    # A: a copper handheld with an amethyst antenna crystal; the screen shows the arrow.
    "a": [
        "..........KK....",
        ".........KAaK...",
        ".........KapK...",
        "..KKKKKKKKdKKK..",
        ".KCCCCCCCCCCcK..",
        ".KCKKKKKKKKKcdK.",
        ".KCKSsSWSSSKcdK.",
        ".KCKSSWWWSSKcdK.",
        ".KCKSWWWWWSKcdK.",
        ".KCKSSSWSSSKcdK.",
        ".KCKSSSWSSSKcdK.",
        ".KcKKKKKKKKKcdK.",
        ".KcvcAccvcAcddK.",
        ".KccccccccccddK.",
        "..KddddddddddK..",
        "...KKKKKKKKKK...",
    ],
    # B: a slim deepslate slab under a glowing sculk lens: Dave's eye.
    "b": [
        "......KKKKK.....",
        ".....KqtTtqK....",
        ".....KtTWTtK....",
        ".....KqtTtqK....",
        "...KKKKKKKKKKK..",
        "...KDDDDDDDDdK..",
        "...KDKKKKKKKdK..",
        "...KDKqqTqqKdK..",
        "...KDKqTTTqKdK..",
        "...KDKTTTTTKdK..",
        "...KDKqqTqqKdK..",
        "...KDKqqTqqKdK..",
        "...KDKKKKKKKdK..",
        "...KdHdhdHdxxK..",
        "...KxxxxxxxxxK..",
        "....KKKKKKKKK...",
    ],
    # C: a flip-open quartz case, gold grille lid up, a redstone arrow on the screen.
    "c": [
        "...KKKKKKKKKKK..",
        "...KYYYYYYYYyK..",
        "...KYoYoYoYoyK..",
        "...KyyyyyyyyoK..",
        "....KKKKKKKKK...",
        "....KoYYYYYoK...",
        "...KKKKKKKKKKK..",
        "...KQQQQQQQQqK..",
        "...KQKKKKKKKqK..",
        "...KQKssRssKqK..",
        "...KQKsRRRsKqK..",
        "...KQKRRRRRKqK..",
        "...KQKssRssKqK..",
        "...KQKKKKKKKqK..",
        "...KqRqYqYqqnK..",
        "....KKKKKKKKK...",
    ],
}


# ---- Power Packs ----------------------------------------------------------------------
def pack_a():
    # A tall copper-capped cell: a battery silhouette, the eye's green sealed in glass.
    return [
        "................",
        "......KKKK......",
        ".....KCccdK.....",
        "...KKKKKKKKKK...",
        "...KCCCcccddK...",
        "...KKKKKKKKKK...",
        "...KvWEEEEgvK...",
        "...KvEekkeEvK...",
        "...KvEekkegvK...",
        "...KveekkegvK...",
        "...KvgeeeggvK...",
        "...KKKKKKKKKK...",
        "...KCCCcccddK...",
        "...KcccccdddK...",
        "....KKKKKKKK....",
        "................",
    ]


def pack_b():
    # A deepslate-and-echo diamond holding the eye, sculk glow at its four points.
    c = blank()
    for y in range(N):
        for x in range(N):
            d = abs(x - 7.5) + abs(y - 7.5)
            if d > 7:
                continue
            if d == 7:
                c[y][x] = "K"
            elif d == 6:
                c[y][x] = "H" if x + y < 15 else "h"
            elif d == 5:
                c[y][x] = "q"
            else:
                c[y][x] = "E" if x + y <= 13 and d >= 3 else ("e" if x + y <= 16 else "g")
    for y in range(6, 10):
        for x in (7, 8):
            c[y][x] = "k"
    c[5][6] = "w"
    for x, y in ((7, 1), (8, 1), (1, 7), (1, 8), (14, 7), (14, 8), (7, 14), (8, 14)):
        c[y][x] = "T"
    return rows(c)


def pack_c():
    # An octagonal quartz cell, gold-ringed eye lens, gold rivets, a redstone status light.
    c = blank()
    for y in range(N):
        for x in range(N):
            dx, dy = abs(x - 7.5), abs(y - 7.5)
            if max(dx, dy) <= 6.5 and dx + dy <= 9:
                c[y][x] = "Q" if x + y < 13 else ("q" if x + y < 19 else "n")
    c = outline(c)
    for y in range(N):
        for x in range(N):
            r2 = (x - 7.5) ** 2 + (y - 7.5) ** 2
            if r2 <= 3.2**2:
                c[y][x] = "E" if x + y <= 13 else ("e" if x + y <= 16 else "g")
            elif r2 <= 4.3**2:
                c[y][x] = "Y" if x + y < 15 else "y"
    for y in range(6, 10):
        for x in (7, 8):
            c[y][x] = "k"
    c[6][6] = "w"
    for x, y in ((3, 3), (12, 3), (3, 12), (12, 12)):
        c[y][x] = "o"
    c[2][4] = c[2][11] = "Y"
    c[13][7], c[13][8] = "R", "r"
    return rows(c)


PACK = {"a": pack_a(), "b": pack_b(), "c": pack_c()}


# ---- Replicator faces -----------------------------------------------------------------
def noise(seed, choices):
    rnd = random.Random(seed)
    return [[rnd.choice(choices) for _ in range(N)] for _ in range(N)]


def body(v, seed):
    if v == "a":  # tuff: the stone the trial chambers were built from
        return noise(seed, "tttTu")
    if v == "b":  # deepslate tiles, offset rows
        c = noise(seed, "dddDx")
        for y in range(N):
            for x in range(N):
                if y % 4 == 3 or (x + (4 if (y // 4) % 2 else 0)) % 8 == 7:
                    c[y][x] = "x"
        return c
    c = noise(seed, "QQQq")  # quartz, faint pillar grain
    for y in range(N):
        for x in (4, 11):
            c[y][x] = "q"
    return c


TRIM = {"a": ("C", "d"), "b": ("H", "q"), "c": ("Y", "o")}
LIGHT = {"a": ("g", "E"), "b": ("q", "T"), "c": ("r", "R")}
WINDOW_EMPTY = {"a": ("S", "s"), "b": ("x", "q"), "c": ("s", "o")}


def frame(c, v):
    hi, lo = TRIM[v]
    for i in range(N):
        c[0][i] = hi
        c[i][0] = hi
        c[N - 1][i] = lo
        c[i][N - 1] = lo


def box(c, x0, y0, x1, y1, ch):
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            c[y][x] = ch


def front(v, programmed):
    c = body(v, 11)
    frame(c, v)
    # The program window: empty until Dave teaches it one thing, then a swatch of it.
    box(c, 4, 2, 11, 7, "K")
    if programmed:
        box(c, 5, 3, 10, 6, "i")
        box(c, 5, 3, 10, 3, "I")
        box(c, 5, 3, 5, 6, "I")
        box(c, 6, 6, 10, 6, "j")
        box(c, 10, 4, 10, 6, "j")
        c[4][7] = c[5][8] = "j"
    else:
        dark, mid = WINDOW_EMPTY[v]
        box(c, 5, 3, 10, 6, dark)
        for x in range(5, 11):
            for y in range(3, 7):
                if (x + y) % 3 == 0:
                    c[y][x] = mid
    lit = LIGHT[v][1 if programmed else 0]
    for y in (3, 4, 5):
        c[y][2] = c[y][13] = lit
    # The furnace it was: the mouth, barred in the variant's trim.
    box(c, 3, 9, 12, 13, "K")
    box(c, 4, 10, 11, 12, "k" if v != "b" else "K")
    hi, lo = TRIM[v]
    for x in (5, 8, 10):
        box(c, x, 10, x, 12, lo)
    if v == "b":
        box(c, 4, 12, 11, 12, "t")
    if v == "a":
        c[1][7] = c[1][8] = "a"
    return rows(c)


def side(v):
    c = body(v, 23)
    frame(c, v)
    hi, lo = TRIM[v]
    box(c, 3, 11, 12, 12, "K")
    box(c, 3, 11, 12, 11, lo)
    for x in range(4, 12, 2):
        c[12][x] = hi
    return rows(c)


def top(v):
    # The recipe, seen from above: eight Power Packs around the furnace.
    c = body(v, 37)
    frame(c, v)
    for gy, y0 in enumerate((1, 6, 11)):
        for gx, x0 in enumerate((1, 6, 11)):
            box(c, x0, y0, x0 + 3, y0 + 3, "K")
            if gx == gy == 1:
                box(c, x0 + 1, y0 + 1, x0 + 2, y0 + 2, TRIM[v][1])
            else:
                c[y0 + 1][x0 + 1] = "E"
                c[y0 + 1][x0 + 2] = "e"
                c[y0 + 2][x0 + 1] = "e"
                c[y0 + 2][x0 + 2] = "g"
    return rows(c)


def half(im):
    """Halve a face to 8x8, keeping its features: each 2x2 block keeps its odd colour out.

    An iso face is only 8 px wide, so averaging smears the window and mouth into the stone
    and point-sampling drops half of them; preferring whatever isn't the face's ground
    colour keeps the window, the mouth and the lights readable.
    """
    px = im.load()
    counts = {}
    for y in range(N):
        for x in range(N):
            counts[px[x, y]] = counts.get(px[x, y], 0) + 1
    ground = max(counts, key=counts.get)
    out = Image.new("RGBA", (N // 2, N // 2))
    for y in range(0, N, 2):
        for x in range(0, N, 2):
            block = [px[x + dx, y + dy] for dy in (0, 1) for dx in (0, 1)]
            odd = [c for c in block if c != ground]
            pick = max(set(odd), key=odd.count) if len(odd) >= 2 else ground
            out.putpixel((x // 2, y // 2), pick)
    return out


def iso(front_im, side_im, top_im):
    """A 16-px block icon the way the inventory shows a block: top, front-left, right side."""
    big = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    faces = (
        (top_im, 1.0, "top"),
        (half(front_im), 0.86, "left"),
        (half(side_im), 0.66, "right"),
    )
    px = big.load()
    srcs = {name: (im.load(), im.width, k) for im, k, name in faces}
    for Y in range(N):
        for X in range(N):
            x, y = X + 0.5, Y + 0.5
            hit = None
            # top rhombus (8,0) (16,4) (8,8) (0,4); left face (0,4)..(8,16); right (8,8)..(16,16)
            a = ((x - 8) / 8 + y / 4) / 2  # along (8,0)->(16,4)
            b = (y / 4 - (x - 8) / 8) / 2  # along (8,0)->(0,4)
            if 0 <= a < 1 and 0 <= b < 1:
                hit = ("top", a, b)
            elif x < 8:
                u = x / 8
                v = (y - 4 - 4 * u) / 8
                if 0 <= v < 1:
                    hit = ("left", u, v)
            else:
                u = (x - 8) / 8
                v = (y - 8 + 4 * u) / 8
                if 0 <= v < 1:
                    hit = ("right", u, v)
            if hit:
                name, u, v = hit
                src, w, k = srcs[name]
                r, g, bb, al = src[min(int(u * w), w - 1), min(int(v * w), w - 1)]
                px[X, Y] = (int(r * k), int(g * k), int(bb * k), al)
    return big


# ---- vanilla stand-ins (our approximations, for contrast only) ------------------------
VPAL = {
    ".": None,
    "K": "#0d2a22", "e": "#2e8c63", "E": "#8ad9a5", "g": "#17553c", "k": "#081510",
    "P": "#0b2b2c", "p": "#1d5f5a", "m": "#2f8a7f", "M": "#7fd6c2", "z": "#103d3c",
    "O": "#2a2a2a", "s": "#8e8e8e", "S": "#cfcfcf", "f": "#5e5e5e", "r": "#d8261f",
    "w": "#f2f2f2", "b": "#c9b48a",
    "C": "#e59a6e", "c": "#b8684a", "d": "#7c3d2b", "A": "#d7b0ff", "a": "#9a72d6",
    "x": "#1a1a1a", "F": "#ff9f1c",
}


def disc(r, fill):
    c = blank()
    for y in range(N):
        for x in range(N):
            if (x - 7.5) ** 2 + (y - 7.5) ** 2 <= r * r:
                c[y][x] = fill(x, y)
    return c


def ender_eye():
    c = disc(5.6, lambda x, y: "E" if x + y < 12 else ("e" if x + y < 18 else "g"))
    c = outline(c)
    for y in range(6, 10):
        c[y][7] = c[y][8] = "k"
    c[4][5] = "E"
    return rows(c)


def ender_pearl():
    c = disc(5.6, lambda x, y: "m" if x + y < 12 else ("p" if x + y < 18 else "z"))
    c = outline(c, "P")
    c[5][5] = c[5][6] = c[6][5] = "M"
    c[8][9] = c[9][8] = "z"
    return rows(c)


def compass():
    c = disc(6.2, lambda x, y: "S" if x + y < 13 else "s")
    c = outline(c, "O")
    for y in range(N):
        for x in range(N):
            if c[y][x] not in ".O" and (x - 7.5) ** 2 + (y - 7.5) ** 2 <= 3.6**2:
                c[y][x] = "b"
    for i, (x, y) in enumerate(((10, 5), (9, 6), (8, 7), (7, 8), (6, 9), (5, 10))):
        c[y][x] = "r" if i < 3 else "w"
    return rows(c)


def spyglass():
    c = blank()
    for i in range(10):
        x, y = 3 + i, 12 - i
        for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            c[y + dy - 1][x + dx] = "C" if dy == 0 else ("c" if dx == 0 else "d")
    c = outline(c, "K")
    for x, y in ((12, 2), (13, 2), (12, 3), (13, 3)):
        c[y][x] = "A"
    c[3][13] = "a"
    for x, y in ((3, 11), (4, 11), (3, 12)):
        c[y][x] = "x"
    return rows(c)


def furnace_faces():
    st = noise(5, "ssSf")
    fr = [r[:] for r in st]
    box(fr, 4, 8, 11, 13, "x")
    box(fr, 5, 12, 10, 12, "F")
    top_ = [r[:] for r in noise(9, "ssSf")]
    return rows(fr), rows(st), rows(top_)


# ---- build ----------------------------------------------------------------------------
def build():
    out = {}
    for v in "abc":
        pal = PAL[v]
        d = HERE / v
        d.mkdir(exist_ok=True)
        f0, f1 = render(front(v, False), pal), render(front(v, True), pal)
        sd, tp = render(side(v), pal), render(top(v), pal)
        ims = {
            "tricorder": render(TRICORDER[v], pal),
            "power_pack": render(PACK[v], pal),
            "replicator": iso(f0, sd, tp),
            "replicator_iron": iso(f1, sd, tp),
            "replicator_front": f0,
            "replicator_front_iron": f1,
            "replicator_side": sd,
            "replicator_top": tp,
        }
        for name, im in ims.items():
            im.save(d / f"{name}.png")
        out[v] = ims
    vd = HERE / "vanilla"
    vd.mkdir(exist_ok=True)
    ff, fs, ft = (render(g, VPAL) for g in furnace_faces())
    van = {
        "ender_eye": render(ender_eye(), VPAL),
        "ender_pearl": render(ender_pearl(), VPAL),
        "compass": render(compass(), VPAL),
        "spyglass": render(spyglass(), VPAL),
        "furnace": iso(ff, fs, ft),
    }
    faces = {"furnace_front": ff, "furnace_side": fs, "furnace_top": ft}
    for name, im in {**van, **faces}.items():
        im.save(vd / f"{name}.png")
    return out, van


def contact(out, van, dest):
    names = ["tricorder", "power_pack", "replicator", "replicator_iron",
             "replicator_front", "replicator_front_iron", "replicator_side", "replicator_top"]
    s, pad, lab = 8, 12, 110
    cw = N * s + pad
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    rows_ = [(f"Variant {v.upper()}", [out[v][n] for n in names]) for v in "abc"]
    rows_.append(("Vanilla (ours)", list(van.values())))
    W = lab + cw * len(names) + pad
    H = 28 + len(rows_) * (N * s + 34) + 70
    sheet = Image.new("RGBA", (W, H), (40, 42, 46, 255))
    dr = ImageDraw.Draw(sheet)
    for i, n in enumerate(names):
        dr.text((lab + i * cw, 8), n.replace("replicator_", "rep. "), fill="#e6e7e9", font=font)
    y = 28
    for title, ims in rows_:
        dr.text((8, y + 56), title, fill="#e6e7e9", font=font)
        for i, im in enumerate(ims):
            x = lab + i * cw
            dr.rectangle([x, y, x + N * s - 1, y + N * s - 1], fill=(139, 139, 139, 255))
            sheet.alpha_composite(im.resize((N * s, N * s), Image.Resampling.NEAREST), (x, y))
        y += N * s + 34
    # A 1x strip: each variant's pack between the eye and the pearl, as a hotbar meets them.
    dr.text((8, y + 2), "1x / 2x", fill="#e6e7e9", font=font)
    x = lab
    for scale in (1, 2):
        for v in "abc":
            for im in (van["ender_eye"], out[v]["power_pack"], van["ender_pearl"]):
                sheet.alpha_composite(im.resize((N * scale,) * 2, Image.Resampling.NEAREST),
                                      (x, y))
                x += N * scale + 4
            x += 14
        x += 20
    sheet.save(Path(dest) / "contact.png")


if __name__ == "__main__":
    o, vn = build()
    if len(sys.argv) > 1:
        contact(o, vn, sys.argv[1])
