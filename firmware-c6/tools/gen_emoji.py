#!/usr/bin/env python3
"""Builds the panel's colour emoji from Noto Color Emoji's PNGs (README "Emoji").

    python3 firmware-c6/tools/gen_emoji.py            # rewrite components/ui_kit/emoji/*.c
    python3 firmware-c6/tools/gen_emoji.py --check    # exit 1 if the checked-in files are stale
    python3 firmware-c6/tools/gen_emoji.py --pin      # record the checksum of a newly listed one

`components/ui_kit/emoji/emoji_set.txt` names exactly which emoji the panel carries, each with
the SHA-256 of its source PNG. The sources are Noto Color Emoji's `png/128/` images at the tag
below (Apache 2.0, NOTICE in that directory), fetched once into a cache outside the repo and
refused when a checksum differs, so a moved tag cannot change a pixel. Only the generated C is
committed: the IDF build never needs the network and two clean builds stay byte-identical.

Two sizes are cut, not one per font (README "Emoji": flash against quality): 18 px for text up
to 20 px and 24 px above it. Each image is an area average of the 128 px source in premultiplied
integer arithmetic, so the same PNGs always make the same bytes; then RGB565A8 (what LVGL blends
straight from flash with alpha), trimmed to its inked box so the transparent margin costs
nothing. Plain Python, no dependencies, like gen_jerv_mark.py.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import unicodedata
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "firmware-c6" / "components" / "ui_kit" / "emoji"
LIST = DIR / "emoji_set.txt"
OUT_SET = DIR / "emoji_set.c"
OUT_IMG = DIR / "emoji_img.c"

TAG = "v2.051"
URL = "https://raw.githubusercontent.com/googlefonts/noto-emoji/{tag}/png/128/emoji_u{cp:x}.png"
SRC = 128
#: The cut sizes. ui_emoji.c maps each font size to one; a new one is a new tier there too.
TIERS = (18, 24)
#: The unknown-emoji mark (README "Emoji"): a dim disc, not a box.
PLACE_RGB = (0x8E, 0x8E, 0x93)
PLACE_ALPHA = 150
PLACE_DIAMETER = 0.42


def cache_dir() -> Path:
    base = os.environ.get("JBRAIN_EMOJI_CACHE") or Path.home() / ".cache" / "jbrain-emoji"
    return Path(base) / f"noto-emoji-{TAG}"


# ---- the list ---------------------------------------------------------------------------------


def read_list() -> list[tuple[int, str]]:
    """(code point, sha256 or "") in the file's order; comments and blank lines skipped."""
    out: list[tuple[int, str]] = []
    for n, line in enumerate(LIST.read_text(encoding="utf-8").splitlines(), 1):
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        fields = body.split()
        cp = int(fields[0], 16)
        digest = fields[1] if len(fields) > 1 and len(fields[1]) == 64 else ""
        assert cp not in {c for c, _ in out}, f"emoji_set.txt:{n}: U+{cp:X} listed twice"
        out.append((cp, digest))
    return out


def pin_list(digests: dict[int, str]) -> None:
    """Rewrites each entry as `cp sha256 char name`, keeping comments and order."""
    lines = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        body = line.split("#", 1)[0].strip()
        if not body:
            lines.append(line)
            continue
        cp = int(body.split()[0], 16)
        name = unicodedata.name(chr(cp), "").lower()
        lines.append(f"{cp:<6x} {digests[cp]}  {chr(cp)}  {name}".rstrip())
    LIST.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- the sources ------------------------------------------------------------------------------


def source(cp: int, want: str) -> bytes:
    path = cache_dir() / f"emoji_u{cp:x}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL.format(tag=TAG, cp=cp), timeout=60) as r:
            data = r.read()
        path.write_bytes(data)
    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if want and got != want:
        sys.exit(f"U+{cp:X}: {path} has sha256 {got}, emoji_set.txt pins {want}")
    return data


def decode_png(data: bytes) -> list[bytearray]:
    """An 8-bit RGBA, non-interlaced PNG (every Noto `png/128` image) into rows of RGBA bytes."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, head = 8, bytearray(), None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos : pos + 8])
        chunk = data[pos + 8 : pos + 8 + n]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        pos += 12 + n
    assert head, "no IHDR"
    w, h, depth, ctype, _, _, interlace = head
    assert (depth, ctype, interlace) == (8, 6, 0), f"unsupported PNG {head}"
    raw = zlib.decompress(bytes(idat))
    stride, bpp = w * 4, 4
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        cur = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        else:
            assert f == 0, f"bad filter {f}"
        rows.append(cur)
        prev = cur
    assert len(rows) == SRC and w == SRC, f"expected {SRC} px, got {w}x{h}"
    return rows


# ---- the cut ----------------------------------------------------------------------------------


def taps(size: int) -> list[list[tuple[int, int]]]:
    """For each output pixel along one axis, (source pixel, overlap) in units where a source
    pixel is `size` long and an output pixel SRC long: every weight is an integer."""
    out = []
    for o in range(size):
        lo, hi = o * SRC, (o + 1) * SRC
        row = []
        for i in range(lo // size, min(SRC, (hi + size - 1) // size)):
            w = min(hi, (i + 1) * size) - max(lo, i * size)
            if w > 0:
                row.append((i, w))
        out.append(row)
    return out


def rdiv(num: int, den: int) -> int:
    return (2 * num + den) // (2 * den)


def cut(rows: list[bytearray], size: int) -> list[tuple[int, int, int, int]]:
    """RGBA pixels (straight alpha) of the area-averaged image, row-major."""
    t = taps(size)
    # Horizontal pass, premultiplied: per source row, per output column, (a, ra, ga, ba).
    horiz = []
    for row in rows:
        line = []
        for col in t:
            a = r = g = b = 0
            for i, w in col:
                alpha = row[4 * i + 3] * w
                a += alpha
                r += row[4 * i] * alpha
                g += row[4 * i + 1] * alpha
                b += row[4 * i + 2] * alpha
            line.append((a, r, g, b))
        horiz.append(line)
    total = SRC * SRC
    out = []
    for trow in t:
        for x in range(size):
            a = r = g = b = 0
            for j, w in trow:
                pa, pr, pg, pb = horiz[j][x]
                a += pa * w
                r += pr * w
                g += pg * w
                b += pb * w
            alpha = rdiv(a, total)
            if alpha == 0 or a == 0:
                out.append((0, 0, 0, 0))
            else:
                out.append((rdiv(r, a), rdiv(g, a), rdiv(b, a), alpha))
    return out


def placeholder(size: int) -> list[tuple[int, int, int, int]]:
    """The dim disc an unknown emoji draws, 8 x 8 supersampled."""
    ss, rad = 8, PLACE_DIAMETER * size / 2
    c = size / 2
    out = []
    for y in range(size):
        for x in range(size):
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    dx = x + (sx + 0.5) / ss - c
                    dy = y + (sy + 0.5) / ss - c
                    hits += dx * dx + dy * dy <= rad * rad
            alpha = rdiv(hits * PLACE_ALPHA, ss * ss)
            out.append((*PLACE_RGB, alpha) if alpha else (0, 0, 0, 0))
    return out


def trim(px: list[tuple[int, int, int, int]], size: int) -> tuple[int, int, int, int, bytes]:
    """(x0, y0, w, h, RGB565A8 bytes) of the inked box; a blank image keeps one clear pixel."""
    ink = [(i % size, i // size) for i, p in enumerate(px) if p[3]]
    if not ink:
        return 0, 0, 1, 1, bytes(3)
    x0, x1 = min(x for x, _ in ink), max(x for x, _ in ink)
    y0, y1 = min(y for _, y in ink), max(y for _, y in ink)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    colour, alpha = bytearray(), bytearray()
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            r, g, b, a = px[y * size + x]
            v = (rdiv(r * 31, 255) << 11) | (rdiv(g * 63, 255) << 5) | rdiv(b * 31, 255)
            colour += struct.pack("<H", v)
            alpha.append(a)
    return x0, y0, w, h, bytes(colour + alpha)


# ---- the C ------------------------------------------------------------------------------------


def c_bytes(data: bytes) -> str:
    # Decimal and unpadded: the files are mostly clear pixels, and "0," is the cheapest byte.
    lines, line = [], ""
    for b in data:
        item = f"{b},"
        if len(line) + len(item) > 96:
            lines.append(line)
            line = ""
        line += item
    if line:
        lines.append(line)
    return "\n".join(lines)


def render(entries: list[tuple[int, str]]) -> tuple[str, str, dict[int, str]]:
    digests: dict[int, str] = {}
    cps = sorted(cp for cp, _ in entries)
    want = dict(entries)
    rows_by_cp = {}
    for cp in cps:
        data = source(cp, want[cp])
        digests[cp] = hashlib.sha256(data).hexdigest()
        rows_by_cp[cp] = decode_png(data)
    list_sum = hashlib.sha256(LIST.read_bytes()).hexdigest()[:16]

    head = (
        "/* GENERATED by firmware-c6/tools/gen_emoji.py from emoji_set.txt — do not edit.\n"
        f" * Noto Color Emoji {TAG} (Apache 2.0, see NOTICE). List sha256 {list_sum}. */\n"
    )
    set_c = [head, '#include "emoji_logic.h"\n\n']
    set_c.append(f"const uint16_t EMOJI_SET_N = {len(cps)};\n\n")
    set_c.append(f"const uint32_t EMOJI_SET[{len(cps)}] = {{\n")
    for i in range(0, len(cps), 8):
        set_c.append("    " + " ".join(f"0x{cp:05X}," for cp in cps[i : i + 8]) + "\n")
    set_c.append("};\n")

    img_c = [head, '#include "ui_emoji.h"\n']
    total = 0
    for size in TIERS:
        blob = bytearray()
        dscs, glyphs = [], []
        images = [cut(rows_by_cp[cp], size) for cp in cps] + [placeholder(size)]
        for px in images:
            x0, y0, w, h, data = trim(px, size)
            off = len(blob)
            blob += data
            blob += bytes(-len(blob) % 4)
            dscs.append((w, h, off, len(data)))
            glyphs.append((x0, y0))
        total += len(blob)
        img_c.append(
            f"\n/* {size} px: {len(images)} images (the last is the unknown mark), "
            f"{len(blob)} bytes. */\n"
            f"static const LV_ATTRIBUTE_MEM_ALIGN uint8_t PX_{size}[{len(blob)}] = {{\n"
            f"{c_bytes(bytes(blob))}\n}};\n"
        )
        img_c.append(f"\nstatic const lv_image_dsc_t IMG_{size}[{len(images)}] = {{\n")
        for w, h, off, n in dscs:
            img_c.append(
                f"    {{.header = {{.magic = LV_IMAGE_HEADER_MAGIC, .cf = LV_COLOR_FORMAT_RGB565A8,"
                f" .w = {w}, .h = {h}, .stride = {2 * w}}}, .data_size = {n},"
                f" .data = PX_{size} + {off}}},\n"
            )
        img_c.append("};\n")
        img_c.append(f"\nstatic const ui_emoji_at_t AT_{size}[{len(images)}] = {{\n")
        for i in range(0, len(glyphs), 8):
            img_c.append(
                "    " + " ".join(f"{{{x}, {y}}}," for x, y in glyphs[i : i + 8]) + "\n"
            )
        img_c.append("};\n")
    img_c.append(f"\nconst ui_emoji_tier_t UI_EMOJI_TIERS[{len(TIERS)}] = {{\n")
    for size in TIERS:
        img_c.append(f"    {{{size}, IMG_{size}, AT_{size}}},\n")
    img_c.append("};\n")
    print(f"{len(cps)} emoji, {len(TIERS)} sizes, {total} bytes of pixels", file=sys.stderr)
    return "".join(set_c), "".join(img_c), digests


def main() -> int:
    entries = read_list()
    if "--pin" in sys.argv:
        _, _, digests = render([(cp, d) for cp, d in entries])
        pin_list(digests)
        entries = read_list()
    missing = [f"U+{cp:X}" for cp, d in entries if not d]
    if missing:
        print(f"no checksum for {', '.join(missing)}: run with --pin", file=sys.stderr)
        return 1
    set_c, img_c, _ = render(entries)
    if "--check" in sys.argv:
        stale = [p.name for p, t in ((OUT_SET, set_c), (OUT_IMG, img_c))
                 if not p.exists() or p.read_text(encoding="utf-8") != t]
        if stale:
            print(f"{', '.join(stale)} stale: run firmware-c6/tools/gen_emoji.py", file=sys.stderr)
            return 1
        print(f"emoji C matches emoji_set.txt ({len(entries)} emoji)")
        return 0
    OUT_SET.write_text(set_c, encoding="utf-8")
    OUT_IMG.write_text(img_c, encoding="utf-8")
    print(f"wrote {OUT_SET.name}, {OUT_IMG.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
