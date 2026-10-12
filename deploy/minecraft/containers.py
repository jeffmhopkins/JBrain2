"""What's inside the world's chests: "which chest has my diamonds?" (plan §M23, owner
2026-10-12).

Every chunk keeps its block entities in one `BlockEntity` record (key tag 49), the
little-endian NBT compounds written back to back. A container's compound carries its
`id` ("Chest", "Barrel", "ShulkerBox", …), its `x`/`y`/`z`, and an `Items` list whose
entries name the item (`Name`) and its `Count`. A shulker box carried as an item holds
its own `Items` under `tag`, read one level down. Item frames hold one `Item`, a
decorated pot one `item`, a lectern its `book`.

Two chest halves are one chest to a player, so a pair (`pairx`/`pairz`) is merged and
reported at one of its halves. A loot chest nobody has opened has only its `LootTable`:
its contents don't exist until it's opened (as ores before a chunk generates), so it is
skipped and counted. Ender chests are the player's own, not the world's, and are never
read (owner decision).
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import leveldb
import nbt

BLOCK_ENTITY = 49
REREAD_S = 60.0  # as the block index's: a live world's log moves every few seconds

# A block entity id → what a player calls it. Ids not here hold nothing to find.
KINDS = {
    "Chest": "chest",
    "Barrel": "barrel",
    "ShulkerBox": "shulker box",
    "Hopper": "hopper",
    "Dispenser": "dispenser",
    "Dropper": "dropper",
    "Furnace": "furnace",
    "BlastFurnace": "blast furnace",
    "Smoker": "smoker",
    "BrewingStand": "brewing stand",
    "Crafter": "crafter",
    "ChiseledBookshelf": "chiseled bookshelf",
    "ItemFrame": "item frame",
    "GlowItemFrame": "glow item frame",
    "DecoratedPot": "decorated pot",
    "Lectern": "lectern",
}
# Where a one-item holder keeps it.
_SINGLE = {"ItemFrame": "Item", "GlowItemFrame": "Item", "DecoratedPot": "item", "Lectern": "book"}


@dataclass
class Container:
    dim: int
    x: int
    y: int
    z: int
    kind: str
    name: str = ""  # a CustomName someone gave it
    items: dict[str, int] = field(default_factory=dict)
    # Of `items`, how many are inside shulker boxes in it.
    boxed: dict[str, int] = field(default_factory=dict)


def item_name(raw: Any) -> str:
    name = str(raw or "").strip().lower()
    if not name:
        return ""
    return name if ":" in name else f"minecraft:{name}"


def _count(item: dict) -> int:
    n = item.get("Count", 1)
    return n & 0xFF if isinstance(n, int) else 1


def _add(into: dict[str, int], name: str, n: int) -> None:
    if name and n > 0:
        into[name] = into.get(name, 0) + n


def _stack(c: Container, item: Any) -> None:
    """One item stack into a container, and a shulker box's own contents with it."""
    if not isinstance(item, dict):
        return
    _add(c.items, item_name(item.get("Name")), _count(item))
    tag = item.get("tag")
    inner = tag.get("Items") if isinstance(tag, dict) else None
    if isinstance(inner, list):
        for sub in inner:
            if isinstance(sub, dict):
                n, name = _count(sub), item_name(sub.get("Name"))
                _add(c.items, name, n)
                _add(c.boxed, name, n)


def parse(dim: int, value: bytes) -> tuple[list[tuple[Container, tuple[int, int] | None]], int]:
    """One BlockEntity record's containers, each with the x/z of its paired half (or
    None), and how many unopened loot containers it held. Raises on an unreadable record."""
    out: list[tuple[Container, tuple[int, int] | None]] = []
    unopened = 0
    for be in nbt.read_all(value):
        if not isinstance(be, dict):
            continue
        kind = KINDS.get(str(be.get("id", "")))
        if kind is None:
            continue
        x, y, z = be.get("x"), be.get("y"), be.get("z")
        if not (isinstance(x, int) and isinstance(y, int) and isinstance(z, int)):
            continue
        items = be.get("Items")
        if "LootTable" in be and not items:
            unopened += 1
            continue
        custom = be.get("CustomName")
        name = custom if isinstance(custom, str) else ""
        c = Container(dim, x, y, z, kind, name)
        if isinstance(items, list):
            for item in items:
                _stack(c, item)
        held = _SINGLE.get(str(be.get("id")))
        if held:
            _stack(c, be.get(held))
        px, pz = be.get("pairx"), be.get("pairz")
        pair = (px, pz) if isinstance(px, int) and isinstance(pz, int) else None
        out.append((c, pair))
    return out, unopened


def _merge(found: list[tuple[Container, tuple[int, int] | None]]) -> list[Container]:
    """A double chest's two halves as one container, at the half nearer the world's
    origin corner (lowest x, then z) so it's always the same one."""
    at = {(c.dim, c.x, c.y, c.z): (c, pair) for c, pair in found}
    out: list[Container] = []
    for (dim, x, y, z), (c, pair) in at.items():
        other = at.get((dim, pair[0], y, pair[1])) if pair else None
        if other is None or other[1] != (x, z):  # a pair only when each names the other
            out.append(c)
            continue
        if (x, z) > (other[0].x, other[0].z):
            continue  # the other half carries both
        for name, n in other[0].items.items():
            _add(c.items, name, n)
        for name, n in other[0].boxed.items():
            _add(c.boxed, name, n)
        c.name = c.name or other[0].name
        out.append(c)
    return out


def _is_block_entity(key: bytes) -> bool:
    return len(key) in (9, 13) and key[-1] == BLOCK_ENTITY


def _dim(key: bytes) -> int:
    return struct.unpack_from("<i", key, 8)[0] if len(key) == 13 else 0


class ContainerIndex:
    """Every readable container in the world with what it holds, read from the
    BlockEntity records and kept until the files change (at most every REREAD_S), as
    `blocks.BlockIndex` keeps palettes. Only containers holding something are kept."""

    def __init__(self, db: Path, clock=time.monotonic) -> None:
        self.db = db
        self._clock = clock
        self._read_at: float | None = None
        self._signature: tuple = ()
        self.containers: list[Container] = []
        self.unreadable = 0
        self.unopened = 0
        self.records = 0

    def refresh(self, force: bool = False) -> None:
        """Re-read when the files changed. `force` skips the REREAD_S wait, for right after
        the server was asked to write everything out."""
        now = self._clock()
        if not force and self._read_at is not None and now - self._read_at < REREAD_S:
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
        best: dict[bytes, tuple[int, bytes | None]] = {}
        for key, seq, value in leveldb.entries(self.db, wanted=_is_block_entity):
            have = best.get(key)
            if have is None or seq > have[0]:
                best[key] = (seq, value)
        found: list[tuple[Container, tuple[int, int] | None]] = []
        unreadable = unopened = records = 0
        for key, (_, value) in best.items():
            if value is None:
                continue
            records += 1
            try:
                got, skipped = parse(_dim(key), value)
            except (nbt.NbtError, struct.error, UnicodeDecodeError):
                unreadable += 1  # a gap in the search, not a failure of it
                continue
            found.extend(got)
            unopened += skipped
        self.containers = [c for c in _merge(found) if c.items]
        self.unreadable, self.unopened, self.records = unreadable, unopened, records
        self._signature = signature

    def names(self, dim: int) -> set[str]:
        """Every item name held anywhere in the dimension."""
        return {n for c in self.containers if c.dim == dim for n in c.items}

    def find(
        self, dim: int, x: int, z: int, wanted: set[str], limit: int, radius: int | None
    ) -> tuple[list[dict], dict[str, int], int]:
        """The nearest `limit` containers holding a wanted item within `radius` blocks
        across of (x, z) (the whole dimension when None), nearest first; the total of
        each wanted item across every one in range; and how many there were."""
        hits = []
        totals: dict[str, int] = {}
        for c in self.containers:
            if c.dim != dim:
                continue
            held = {n: c.items[n] for n in wanted if n in c.items}
            if not held:
                continue
            distance = round(math.dist((x, z), (c.x, c.z)))
            if radius is not None and distance > radius:
                continue
            for n, k in held.items():
                totals[n] = totals.get(n, 0) + k
            hits.append((distance, c, held))
        hits.sort(key=lambda h: (h[0], -sum(h[2].values())))
        out = [
            {
                "kind": c.kind,
                "x": c.x,
                "y": c.y,
                "z": c.z,
                "distance": distance,
                "items": held,
                "in_shulker_boxes": {n: c.boxed[n] for n in held if n in c.boxed},
                "name": c.name,
            }
            for distance, c, held in hits[:limit]
        ]
        return out, totals, len(hits)
