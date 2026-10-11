"""Top-down map tiles of a Bedrock world (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M8).

The first layer is the **biome atlas**. It needs no block parsing at all: every generated
chunk stores a `Data3D` record holding its height map and its biome per 4×4×4 cell, which
is everything a hillshaded biome map needs, and is read straight off the world's LevelDB
(`leveldb.py`). Ungenerated ground is left transparent, which is the fog the viewer draws.

A tile is 256×256 pixels. At zoom 0 it is 256×256 blocks, one pixel per block; each zoom
out doubles the blocks per pixel, down to one pixel per chunk. Zoomed-out pixels each
read only the one column they show, so even the widest tile is cheap.

What the record looks like on 1.26 (checked against a real world, plan §M8 notes):
- key: chunk x, chunk z (int32 LE), [dimension int32 when not the overworld], tag 43;
- value: 256 int16 heights, index `z * 16 + x`, each the height above the dimension's
  floor (−64 in the overworld) of the first air above the top block; then one
  palettized biome store per 16-high section, bottom up, `0xFF` meaning "same as below".
"""

from __future__ import annotations

import math
import struct
import time
import zlib
from pathlib import Path

import leveldb

DATA3D = 43
# Worlds last saved before 1.18 keep each chunk's surface in Data2D: 256 heights then 256
# one-byte biomes, both indexed z * 16 + x, heights measured from y=0 rather than the
# 1.18 floor of y=-64. A chunk nobody has loaded since an upgrade keeps it, so an
# imported old world would otherwise be explored ground drawn as never generated.
DATA2D = 45
_LEGACY_FLOOR = 64
TILE = 256  # blocks per tile side at zoom 0
# Zoom k covers 2^k × 256 blocks per side, one pixel per 2^k blocks. The furthest zoom out
# is one pixel per chunk (owner, 2026-10-10). Zoomed-out tiles sample only the columns
# their pixels show, so a 4096-block overview costs 65k cheap lookups, not 65k decodes.
MAX_ZOOM = 4
CHUNKS_PER_TILE = TILE // 16
DIMENSIONS = {"overworld": 0, "nether": 1, "the_end": 2}
_FLOOR = {0: -64, 1: 0, 2: 0}

# Bedrock biome ids → map colours: the familiar atlas palette (greens for forests, tan
# deserts, blue water deepening offshore), so a family member reads it without a legend.
BIOME_COLORS: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 112), 1: (141, 179, 96), 2: (250, 148, 24), 3: (96, 96, 96),
    4: (5, 102, 33), 5: (11, 102, 89), 6: (7, 249, 178), 7: (0, 0, 255),
    8: (191, 59, 59), 9: (128, 128, 255), 10: (112, 112, 214), 11: (160, 160, 255),
    12: (255, 255, 255), 13: (160, 160, 160), 14: (255, 0, 255), 15: (160, 0, 255),
    16: (250, 222, 85), 17: (210, 95, 18), 18: (34, 85, 28), 19: (22, 57, 51),
    20: (114, 120, 154), 21: (83, 123, 9), 22: (44, 66, 5), 23: (98, 139, 23),
    24: (0, 0, 48), 25: (162, 162, 132), 26: (250, 240, 192), 27: (48, 116, 68),
    28: (31, 95, 50), 29: (64, 81, 26), 30: (49, 85, 74), 31: (36, 63, 54),
    32: (89, 102, 81), 33: (69, 79, 62), 34: (80, 112, 80), 35: (189, 178, 95),
    36: (167, 157, 100), 37: (217, 69, 21), 38: (176, 151, 101), 39: (202, 140, 101),
    40: (0, 0, 172), 41: (0, 0, 80), 42: (0, 0, 144), 43: (0, 0, 64),
    44: (32, 32, 112), 45: (32, 32, 56), 46: (112, 112, 214), 47: (64, 64, 144),
    48: (118, 142, 20), 49: (59, 71, 10), 129: (181, 219, 136), 130: (255, 188, 64),
    131: (136, 136, 136), 132: (45, 142, 73), 133: (51, 142, 129), 134: (47, 255, 218),
    140: (180, 220, 220), 149: (123, 163, 49), 151: (138, 179, 63), 155: (88, 156, 108),
    156: (71, 135, 90), 157: (104, 121, 66), 158: (89, 125, 114), 160: (129, 142, 121),
    161: (109, 119, 102), 162: (120, 152, 120), 163: (229, 218, 135),
    164: (207, 197, 140), 165: (255, 109, 61), 166: (216, 191, 141),
    167: (242, 180, 141), 177: (172, 141, 96), 178: (94, 56, 48), 179: (221, 8, 8),
    180: (73, 144, 123), 181: (64, 54, 54), 182: (220, 220, 200), 183: (176, 179, 206),
    184: (196, 196, 196), 185: (71, 114, 108), 186: (131, 187, 109),
    187: (40, 110, 40), 188: (96, 82, 60), 189: (123, 143, 116), 190: (14, 30, 40),
    191: (44, 204, 142), 192: (255, 145, 200), 193: (110, 120, 110),
}
_UNKNOWN = (128, 128, 128)
WATER_BIOMES = frozenset({0, 7, 10, 11, 24, 40, 41, 42, 43, 44, 45, 46, 47})


def chunk_key(cx: int, cz: int, dim: int, tag: int) -> bytes:
    head = struct.pack("<ii", cx, cz)
    return head + (struct.pack("<i", dim) if dim else b"") + bytes([tag])


def _palette_store(buf: bytes, pos: int) -> tuple[list[int] | None, int]:
    """One palettized 16×16×16 store: (values in x,z,y order, next offset), or None for
    the `0xFF` "copy the store below" marker."""
    header = buf[pos]
    pos += 1
    if header == 0xFF:
        return None, pos
    bits = header >> 1
    if bits == 0:
        (only,) = struct.unpack_from("<i", buf, pos)
        return [only] * 4096, pos + 4
    per_word = 32 // bits
    words = -(-4096 // per_word)
    packed = struct.unpack_from(f"<{words}I", buf, pos)
    pos += 4 * words
    (size,) = struct.unpack_from("<i", buf, pos)
    pos += 4
    palette = struct.unpack_from(f"<{size}i", buf, pos)
    pos += 4 * size
    mask = (1 << bits) - 1
    out: list[int] = []
    for word in packed:
        for i in range(per_word):
            out.append(palette[(word >> (i * bits)) & mask])
    return out[:4096], pos


def _store_end(buf: bytes, pos: int) -> int:
    """Where the palettized store at `pos` ends, without decoding it."""
    bits = buf[pos] >> 1
    if buf[pos] == 0xFF:
        return pos + 1
    if bits == 0:
        return pos + 5
    words = -(-4096 // (32 // bits))
    pos += 1 + 4 * words
    (size,) = struct.unpack_from("<i", buf, pos)
    return pos + 4 + 4 * size


def surface_sample(value: bytes, x: int = 8, z: int = 8) -> tuple[int, int]:
    """(height, biome) of ONE column, reading only the store that holds its surface —
    the overview's per-chunk pixel, microseconds instead of a full decode."""
    (height,) = struct.unpack_from("<h", value, (z * 16 + x) * 2)
    y = max(height - 1, 0)
    section, pos, last_real = y // 16, 512, 512
    for _ in range(section):
        if pos >= len(value):
            break
        if value[pos] != 0xFF:
            last_real = pos
        pos = _store_end(value, pos)
    if pos >= len(value) or value[pos] == 0xFF:
        pos = last_real  # "same as below": the last real store
    bits = value[pos] >> 1
    if bits == 0:
        return height, struct.unpack_from("<i", value, pos + 1)[0]
    per_word = 32 // bits
    idx = (x << 8) | (z << 4) | (y % 16)
    words = -(-4096 // per_word)
    (word,) = struct.unpack_from("<I", value, pos + 1 + 4 * (idx // per_word))
    slot = (word >> ((idx % per_word) * bits)) & ((1 << bits) - 1)
    return height, struct.unpack_from("<i", value, pos + 1 + 4 * words + 4 + 4 * slot)[0]


class Column:
    __slots__ = ("height", "biome")

    def __init__(self, height: list[int], biome: list[int]) -> None:
        self.height, self.biome = height, biome  # both indexed z * 16 + x


def decode_data2d(value: bytes) -> Column:
    heights = [h + _LEGACY_FLOOR for h in struct.unpack_from("<256h", value, 0)]
    return Column(heights, list(value[512:768]))


def sample_data2d(value: bytes, x: int, z: int) -> tuple[int, int]:
    i = z * 16 + x
    (height,) = struct.unpack_from("<h", value, i * 2)
    return height + _LEGACY_FLOOR, value[512 + i]


def decode_data3d(value: bytes) -> Column:
    """A chunk's surface: the height and the biome AT that height, per column."""
    heights = list(struct.unpack_from("<256h", value, 0))
    stores: list[list[int]] = []
    pos = 512
    while pos < len(value):
        store, pos = _palette_store(value, pos)
        stores.append(store if store is not None else stores[-1])
    biome = []
    for i, h in enumerate(heights):
        x, z = i % 16, i // 16
        y = max(h - 1, 0)  # the top block, one below the first air
        store = stores[min(y // 16, len(stores) - 1)] if stores else [0] * 4096
        biome.append(store[(x << 8) | (z << 4) | (y % 16)])
    return Column(heights, biome)


# A running server appends to its write-ahead log every few seconds, so "the files
# changed" is nearly always true; re-reading on each change would re-read the whole
# database and drop every cached tile on almost every request (issue #1608). A map that
# trails the world by this long is fine.
REREAD_S = 60.0


class WorldIndex:
    """Every generated chunk's surface record for one world, read once and re-read when
    the database's files change, at most every REREAD_S. A tile then costs a dict lookup
    per chunk instead of a pass over the whole database, which for a big world is
    hundreds of MB."""

    def __init__(self, db: Path, clock=time.monotonic) -> None:
        self.db = db
        self._clock = clock
        self._signature: tuple = ()
        self._read_at: float | None = None
        self._raw: dict[tuple[int, int, int], bytes] = {}
        self._legacy: set[tuple[int, int, int]] = set()  # chunks whose record is Data2D

    def _current(self) -> tuple:
        try:
            return tuple(
                sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.db.iterdir())
            )
        except FileNotFoundError:
            return ()

    def refresh(self) -> None:
        now = self._clock()
        if self._read_at is not None and now - self._read_at < REREAD_S:
            return
        signature = self._current()
        if signature == self._signature:
            return
        raw: dict[tuple[int, int, int], bytes] = {}
        legacy: dict[tuple[int, int, int], bytes] = {}
        for key, value in leveldb.read_db(self.db, wanted=_is_surface).items():
            cx, cz = struct.unpack_from("<ii", key)
            dim = struct.unpack_from("<i", key, 8)[0] if len(key) == 13 else 0
            (raw if key[-1] == DATA3D else legacy)[(dim, cx, cz)] = value
        self._legacy = legacy.keys() - raw.keys()  # a re-saved chunk's Data3D is current
        raw.update((cell, legacy[cell]) for cell in self._legacy)
        self._raw, self._signature, self._read_at = raw, signature, now

    def columns(self, dim: int, cx0: int, cz0: int, n: int) -> dict[tuple[int, int], Column]:
        out = {}
        for cx in range(cx0, cx0 + n):
            for cz in range(cz0, cz0 + n):
                value = self._raw.get((dim, cx, cz))
                if value is None:
                    continue
                try:
                    legacy = (dim, cx, cz) in self._legacy
                    out[(cx, cz)] = decode_data2d(value) if legacy else decode_data3d(value)
                except (struct.error, IndexError):
                    continue  # one corrupt chunk is a hole in the map, not a failed tile
        return out

    def samples(
        self, dim: int, cx0: int, cz0: int, n: int, step: int
    ) -> dict[tuple[int, int], tuple[int, int]]:
        """(height, biome) at every `step`-th column of an n×n chunk square, keyed by
        block position — what a zoomed-out tile's pixels show."""
        out: dict[tuple[int, int], tuple[int, int]] = {}
        offsets = range(step // 2, 16, step) if step < 16 else range(8, 9)
        for cx in range(cx0, cx0 + n):
            for cz in range(cz0, cz0 + n):
                value = self._raw.get((dim, cx, cz))
                if value is None:
                    continue
                sample = sample_data2d if (dim, cx, cz) in self._legacy else surface_sample
                try:
                    for lx in offsets:
                        for lz in offsets:
                            out[(cx * 16 + lx, cz * 16 + lz)] = sample(value, lx, lz)
                except (struct.error, IndexError):
                    continue  # one corrupt chunk is a hole in the map
        return out

    def extent(self, dim: int) -> dict[str, int] | None:
        """The block rectangle the world has generated in a dimension, or None."""
        cells = [(cx, cz) for (d, cx, cz) in self._raw if d == dim]
        if not cells:
            return None
        xs, zs = [c[0] for c in cells], [c[1] for c in cells]
        return {
            "min_x": min(xs) * 16,
            "max_x": max(xs) * 16 + 15,
            "min_z": min(zs) * 16,
            "max_z": max(zs) * 16 + 15,
            "chunks": len(cells),
        }


def _is_surface(key: bytes) -> bool:
    return len(key) in (9, 13) and key[-1] in (DATA3D, DATA2D)


def render_biome(columns: dict[tuple[int, int], Column], cx0: int, cz0: int, n: int) -> list[bytearray]:
    """RGBA rows for an n×n chunk square: biome colour, hillshaded from the NW, with
    water darkened by depth. Missing chunks stay transparent."""
    size = n * 16
    heights: dict[tuple[int, int], int] = {}
    for (cx, cz), col in columns.items():
        for i in range(256):
            heights[((cx - cx0) * 16 + i % 16, (cz - cz0) * 16 + i // 16)] = col.height[i]
    rows = [bytearray(size * 4) for _ in range(size)]
    for (cx, cz), col in columns.items():
        bx, bz = (cx - cx0) * 16, (cz - cz0) * 16
        for i in range(256):
            px, pz = bx + i % 16, bz + i // 16
            h = col.height[i]
            r, g, b = BIOME_COLORS.get(col.biome[i], _UNKNOWN)
            west = heights.get((px - 1, pz), h)
            north = heights.get((px, pz - 1), h)
            shade = 1.0 + max(-0.35, min(0.35, ((h - west) + (h - north)) * 0.06))
            if col.biome[i] in WATER_BIOMES:
                shade *= 0.85 + 0.15 * min(1.0, max(0.0, (h - 40) / 88))
            row = rows[pz]
            o = px * 4
            row[o : o + 4] = bytes(
                (min(255, int(r * shade)), min(255, int(g * shade)), min(255, int(b * shade)), 255)
            )
    return rows


def png(rows: list[bytearray]) -> bytes:
    """An RGBA PNG, stdlib only."""
    height, width = len(rows), len(rows[0]) // 4

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def render_sampled(
    samples: dict[tuple[int, int], tuple[int, int]], x0: int, z0: int, size: int, step: int
) -> list[bytearray]:
    """A size×size tile, one pixel per `step` blocks: each pixel's sampled biome, shaded
    against the samples to its west and north. Unsampled ground stays transparent."""
    rows = [bytearray(size * 4) for _ in range(size)]
    gain = 0.06 / step  # the same slope reads about the same at every zoom
    for (bx, bz), (h, biome) in samples.items():
        west = samples.get((bx - step, bz), (h, 0))[0]
        north = samples.get((bx, bz - step), (h, 0))[0]
        shade = 1.0 + max(-0.35, min(0.35, ((h - west) + (h - north)) * gain))
        r, g, b = BIOME_COLORS.get(biome, _UNKNOWN)
        px, pz = (bx - x0) // step, (bz - z0) // step
        o = px * 4
        rows[pz][o : o + 4] = bytes(
            (min(255, int(r * shade)), min(255, int(g * shade)), min(255, int(b * shade)), 255)
        )
    return rows


def tile(index: WorldIndex, dim: int, zoom: int, tx: int, tz: int) -> bytes:
    """One 256×256 PNG tile. Zoom 0 is one pixel per block; zoom k is one pixel per 2^k
    blocks, down to one pixel per chunk. Tile (0, 0) at any zoom starts at block (0, 0)."""
    span = CHUNKS_PER_TILE << zoom
    cx0, cz0 = tx * span, tz * span
    if zoom:
        step = 1 << zoom
        samples = index.samples(dim, cx0, cz0, span, step)
        return png(render_sampled(samples, cx0 * 16, cz0 * 16, TILE, step))
    return png(render_biome(index.columns(dim, cx0, cz0, span), cx0, cz0, span))


def floor_of(dim: int) -> int:
    return _FLOOR.get(dim, 0)


def tile_of_block(x: float, z: float, zoom: int) -> tuple[int, int]:
    span = TILE << zoom
    return math.floor(x / span), math.floor(z / span)
