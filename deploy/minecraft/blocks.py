"""Find blocks in the explored world: "where's the nearest diamond?" (plan §M8, owner
request 2026-10-11).

Every generated chunk stores its blocks as `SubChunkPrefix` records (tag 47), one per
16-high section: per layer a palette of block states (little-endian NBT compounds, each
with a `name`) and 4096 packed indices into it, in x, z, y order. A search reads only the
palettes first — a section whose palette has no wanted block costs nothing more — and
decodes indices only where a wanted block is known to be.

Only explored ground can be searched: ore placement doesn't exist until the game
generates a chunk, so the seed can't predict it the way it predicts biomes.

The format, checked against a real 1.26 world: version byte 9, a layer count, the
section's signed y index, then per layer a header byte (bits per index << 1 | runtime
flag), the packed words (none when bits is 0), a palette size, and the palette.
"""

from __future__ import annotations

import math
import struct
import time
from pathlib import Path

import leveldb

SUBCHUNK = 47
REREAD_S = 60.0  # as the map's index: a live world's log moves every few seconds
# 1 = byte, 2 = short, 3 = int, 4 = long, 5 = float, 6 = double: fixed sizes.
_FIXED = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}


class _Nbt:
    """Just enough little-endian NBT to walk a palette entry and pull out its name."""

    def __init__(self, buf: bytes, pos: int) -> None:
        self.buf, self.pos = buf, pos

    def _string(self) -> str:
        (n,) = struct.unpack_from("<H", self.buf, self.pos)
        self.pos += 2 + n
        return self.buf[self.pos - n : self.pos].decode("utf-8", "replace")

    def _skip(self, kind: int) -> None:
        if kind in _FIXED:
            self.pos += _FIXED[kind]
        elif kind == 8:
            self._string()
        elif kind in (7, 11, 12):  # byte, int, long arrays
            (n,) = struct.unpack_from("<i", self.buf, self.pos)
            self.pos += 4 + n * {7: 1, 11: 4, 12: 8}[kind]
        elif kind == 9:
            inner = self.buf[self.pos]
            (n,) = struct.unpack_from("<i", self.buf, self.pos + 1)
            self.pos += 5
            for _ in range(n):
                self._skip(inner)
        elif kind == 10:
            self.compound()
        else:
            raise ValueError(f"NBT tag {kind}")

    def compound(self) -> str | None:
        """Walk one compound body; its `name` string if it has one."""
        name = None
        while True:
            kind = self.buf[self.pos]
            self.pos += 1
            if kind == 0:
                return name
            key = self._string()
            if kind == 8 and key == "name":
                name = self._string()
            else:
                self._skip(kind)

    def root(self) -> str | None:
        kind = self.buf[self.pos]
        self.pos += 1
        self._string()  # the root's own (empty) name
        if kind != 10:
            raise ValueError("palette entry isn't a compound")
        return self.compound()


def layers(value: bytes) -> tuple[int, list[tuple[int, int, list[str]]]]:
    """(section y, [(bits, words offset, palette names)]) for one subchunk record."""
    if value[0] not in (8, 9):
        raise ValueError(f"subchunk version {value[0]}")
    count, pos = value[1], 2
    sy = struct.unpack_from("<b", value, 2)[0] if value[0] == 9 else 0
    if value[0] == 9:
        pos = 3
    out = []
    for _ in range(count):
        bits = value[pos] >> 1
        pos += 1
        words_at = pos
        if bits:
            pos += 4 * -(-4096 // (32 // bits))
        (size,) = struct.unpack_from("<i", value, pos)
        pos += 4
        nbt = _Nbt(value, pos)
        names = [nbt.root() or "" for _ in range(size)]
        pos = nbt.pos
        out.append((bits, words_at, names))
    return sy, out


def positions(value: bytes, bits: int, words_at: int, wanted: set[int]) -> list[int]:
    """The (x << 8 | z << 4 | y) indices whose palette entry is in `wanted`."""
    if not bits:
        return list(range(4096)) if 0 in wanted else []
    per = 32 // bits
    mask = (1 << bits) - 1
    words = struct.unpack_from(f"<{-(-4096 // per)}I", value, words_at)
    found = []
    for w, word in enumerate(words):
        for j in range(per):
            i = w * per + j
            if i < 4096 and (word >> (j * bits)) & mask in wanted:
                found.append(i)
    return found


def _is_subchunk(key: bytes) -> bool:
    return len(key) in (10, 14) and key[-2] == SUBCHUNK


def _where(key: bytes) -> tuple[int, int, int]:
    cx, cz = struct.unpack_from("<ii", key)
    dim = struct.unpack_from("<i", key, 8)[0] if len(key) == 14 else 0
    return dim, cx, cz


class BlockIndex:
    """Which block names each explored section holds, read from the palettes alone and
    kept until the files change (at most every REREAD_S) — names only, so a big world's
    index stays small. A search visits only sections that hold a wanted block, nearest
    chunk first, reading just those records from the database."""

    def __init__(self, db: Path, clock=time.monotonic) -> None:
        self.db = db
        self._clock = clock
        self._read_at: float | None = None
        self._signature: tuple = ()
        # (dim, cx, cz) -> {section y: names}; one string object per name, shared.
        self._chunks: dict[tuple[int, int, int], dict[int, frozenset[str]]] = {}

    def refresh(self) -> None:
        now = self._clock()
        if self._read_at is not None and now - self._read_at < REREAD_S:
            return
        try:
            signature = tuple(
                sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in self.db.iterdir())
            )
        except FileNotFoundError:
            signature = ()
        self._read_at = now
        if signature == self._signature:
            return
        interned: dict[str, str] = {}
        best: dict[bytes, tuple[int, frozenset[str] | None]] = {}
        for key, seq, value in leveldb.entries(self.db, wanted=_is_subchunk):
            have = best.get(key)
            if have is not None and have[0] >= seq:
                continue
            names: frozenset[str] | None = None
            if value is not None:
                try:
                    _, found = layers(value)
                except (ValueError, struct.error, IndexError, UnicodeDecodeError):
                    continue  # an unreadable section is a gap in the search, not a failure
                names = frozenset(
                    interned.setdefault(n, n) for _, _, palette in found for n in palette
                )
            best[key] = (seq, names)
        chunks: dict[tuple[int, int, int], dict[int, frozenset[str]]] = {}
        for key, (_, names) in best.items():
            if names is not None:
                chunks.setdefault(_where(key), {})[struct.unpack_from("<b", key, len(key) - 1)[0]] = names
        self._chunks, self._signature = chunks, signature

    def names(self, dim: int) -> set[str]:
        """Every block name in the explored dimension — what can be searched for."""
        out: set[str] = set()
        for (d, _, _), sections in self._chunks.items():
            if d == dim:
                for names in sections.values():
                    out |= names
        return out

    def find(
        self, dim: int, x: int, z: int, wanted: set[str], limit: int = 5, radius: int = 2048
    ) -> tuple[list[dict], int]:
        """The nearest `limit` blocks named in `wanted` within `radius` blocks of (x, z),
        nearest first, and how many explored chunks were in range."""
        cx0, cz0 = x // 16, z // 16
        reach = radius // 16 + 1
        in_range = [
            c
            for c in self._chunks
            if c[0] == dim and abs(c[1] - cx0) <= reach and abs(c[2] - cz0) <= reach
        ]

        def centre(c: tuple[int, int, int]) -> float:
            return math.dist((x, z), (c[1] * 16 + 8, c[2] * 16 + 8))

        holding = sorted(
            (c for c in in_range if any(n & wanted for n in self._chunks[c].values())),
            key=centre,
        )
        hits: list[dict] = []
        batch = 16  # read sections a few chunks at a time, nearest first
        for start in range(0, len(holding), batch):
            group = holding[start : start + batch]
            # A chunk's centre can sit up to ~12 blocks further than its nearest block, so
            # stop only once no chunk left can beat the hits already in hand.
            if len(hits) >= limit and centre(group[0]) - 12 > hits[limit - 1]["distance"]:
                break
            keys = {
                _key(dim, cx, cz, sy)
                for (_, cx, cz) in group
                for sy, names in self._chunks[(dim, cx, cz)].items()
                if names & wanted
            }
            for key, value in leveldb.read_keys(self.db, keys).items():
                _, cx, cz = _where(key)
                sy = struct.unpack_from("<b", key, len(key) - 1)[0]
                hits.extend(_section_hits(value, cx, cz, sy, x, z, wanted))
            hits.sort(key=lambda h: h["distance"])
        return [h for h in hits if h["distance"] <= radius][:limit], len(in_range)


def _key(dim: int, cx: int, cz: int, sy: int) -> bytes:
    prefix = struct.pack("<ii", cx, cz) + (struct.pack("<i", dim) if dim else b"")
    return prefix + bytes([SUBCHUNK]) + struct.pack("<b", sy)


def _section_hits(
    value: bytes, cx: int, cz: int, section: int, x: int, z: int, wanted: set[str]
) -> list[dict]:
    """The nearest block of each wanted kind in one section. `section` comes from the
    record's key, the one place every version stores it: a version-8 value has no y of
    its own, so taking it from the value put an old world's deep ores at y 0-15 (#1616)."""
    try:
        _, found = layers(value)
    except (ValueError, struct.error, IndexError, UnicodeDecodeError):
        return []
    best: dict[str, dict] = {}
    for bits, words_at, palette in found:
        idx = {i for i, name in enumerate(palette) if name in wanted}
        if not idx:
            continue
        for i in positions(value, bits, words_at, idx):
            bx, bz = cx * 16 + (i >> 8), cz * 16 + ((i >> 4) & 15)
            name = palette[_palette_index(value, bits, words_at, i)]
            distance = round(math.dist((x, z), (bx, bz)))
            if name not in best or distance < best[name]["distance"]:
                by = section * 16 + (i & 15)
                best[name] = {"block": name, "x": bx, "y": by, "z": bz, "distance": distance}
    return list(best.values())


def _palette_index(value: bytes, bits: int, words_at: int, i: int) -> int:
    if not bits:
        return 0
    per = 32 // bits
    (word,) = struct.unpack_from("<I", value, words_at + 4 * (i // per))
    return (word >> ((i % per) * bits)) & ((1 << bits) - 1)
