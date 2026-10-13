"""Villagers, pets and named mobs in the world's files: "who sells Mending?", "where's my
dog?" (plan §M4e, owner 2026-10-13).

Every entity is its own `actorprefix` + 8-byte-id record holding one NBT compound, and
each chunk has a `digp` record listing the ids of the entities in it. The compound
carries no dimension, so the dimension (and the chunk) comes from the `digp` key: `digp`
+ x + z, with the dimension int after them outside the Overworld. A world last loaded
before 1.18.30 can still hold per-chunk `Entity` records (key tag 50), back-to-back
compounds placed by their own key.

Only candidates are parsed: a villager or wandering trader, anything with a
`CustomName`, or a tamed animal. That is a byte test on the raw value before any parse,
so a world full of cows and bees is dropped as it streams past and never held.

A villager's `Offers` hold every level's trades, written before anyone opens its trade
screen. The owner decided (2026-10-13) that Dave tells only what it offers now, so a
trade above its `TradeTier` is dropped here and only counted.

Portals are one `portals` record (`data.PortalRecords[]`), read on its own.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import containers
import leveldb
import nbt

ACTOR_PREFIX = b"actorprefix"
DIGP = b"digp"
LEGACY_ENTITY = 50
PORTALS = b"portals"

MAX_VILLAGERS = 20_000
MAX_PETS = 5_000
MAX_NAMED = 5_000
MAX_TRADES = 32

# The byte tests (§M4e): a candidate holds one of these before it is parsed. A tamed
# animal's `IsTamed` byte tag is 1; an untamed one still carries the tag, set to 0.
_VILLAGER_IDS = (b"minecraft:villager", b"minecraft:wandering_trader")
_NAMED = b"\x08\x0a\x00CustomName"
_TAMED = b"\x01\x07\x00IsTamed\x01"

VILLAGER_KINDS = {"villager_v2", "villager", "wandering_trader"}
# A villager's `Variant` as its job, when the file has no `PreferredProfession`.
_JOBS = {
    0: "unemployed",
    1: "farmer",
    2: "fisherman",
    3: "shepherd",
    4: "fletcher",
    5: "librarian",
    6: "cartographer",
    7: "cleric",
    8: "armorer",
    9: "weaponsmith",
    10: "toolsmith",
    11: "butcher",
    12: "leatherworker",
    13: "mason",
    14: "nitwit",
}
LEVELS = ("Novice", "Apprentice", "Journeyman", "Expert", "Master")

# Bedrock's numeric enchantment ids (an enchanted book's `tag.ench[].id`).
ENCHANTMENTS = {
    0: "protection",
    1: "fire_protection",
    2: "feather_falling",
    3: "blast_protection",
    4: "projectile_protection",
    5: "thorns",
    6: "respiration",
    7: "depth_strider",
    8: "aqua_affinity",
    9: "sharpness",
    10: "smite",
    11: "bane_of_arthropods",
    12: "knockback",
    13: "fire_aspect",
    14: "looting",
    15: "efficiency",
    16: "silk_touch",
    17: "unbreaking",
    18: "fortune",
    19: "power",
    20: "punch",
    21: "flame",
    22: "infinity",
    23: "luck_of_the_sea",
    24: "lure",
    25: "frost_walker",
    26: "mending",
    27: "binding",
    28: "vanishing",
    29: "impaling",
    30: "riptide",
    31: "loyalty",
    32: "channeling",
    33: "multishot",
    34: "piercing",
    35: "quick_charge",
    36: "soul_speed",
    37: "swift_sneak",
    38: "wind_burst",
    39: "density",
    40: "breach",
}
# What players call a pet, to the kind the file names.
KIND_ALIASES = {"dog": "wolf", "puppy": "wolf", "kitten": "cat", "kitty": "cat", "pony": "horse"}
_CAT_COATS = {
    "white",
    "tuxedo",
    "red",
    "siamese",
    "british",
    "calico",
    "persian",
    "ragdoll",
    "tabby",
    "black",
    "jellie",
}
_PARROT_COATS = {0: "red", 1: "blue", 2: "green", 3: "cyan", 4: "gray"}


@dataclass
class Trade:
    tier: int
    buy: tuple[str, int]
    buy2: tuple[str, int] | None
    sell: tuple[str, int]
    enchantments: list[tuple[str, int]]
    uses: int
    max_uses: int


@dataclass
class Mob:
    uid: int
    kind: str  # the identifier without `minecraft:`
    dim: int
    x: int
    y: int
    z: int
    name: str = ""
    tamed: bool = False
    owner: int | None = None
    sitting: bool = False
    baby: bool = False
    coat: list[str] = field(default_factory=list)
    # A villager's:
    job: str = ""
    level: int = 0
    locked_in: bool = False
    trades: list[Trade] = field(default_factory=list)
    locked_trades: int = 0

    @property
    def villager(self) -> bool:
        return self.kind in VILLAGER_KINDS


def is_candidate(value: bytes) -> bool:
    return _NAMED in value or _TAMED in value or any(v in value for v in _VILLAGER_IDS)


def _short(raw: Any) -> str:
    return str(raw or "").removeprefix("minecraft:")


def _stack(raw: Any) -> tuple[str, int] | None:
    if not isinstance(raw, dict):
        return None
    name = containers.item_name(raw.get("Name"))
    count = raw.get("Count", 1)
    if not name or not isinstance(count, int) or count <= 0:
        return None
    return name, count & 0xFF


def _enchants(stack: Any) -> list[tuple[str, int]]:
    tag = stack.get("tag") if isinstance(stack, dict) else None
    raw = tag.get("ench") if isinstance(tag, dict) else None
    out: list[tuple[str, int]] = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and isinstance(e.get("id"), int):
            lvl = e.get("lvl")
            name = ENCHANTMENTS.get(e["id"], f"enchantment {e['id']}")
            out.append((name, lvl if isinstance(lvl, int) else 1))
    return out


def _trades(d: dict[str, Any], level: int) -> tuple[list[Trade], int]:
    """The trades it offers now (tier ≤ its level), at most MAX_TRADES, and how many
    locked ones were dropped."""
    offers = d.get("Offers")
    recipes = offers.get("Recipes") if isinstance(offers, dict) else None
    out: list[Trade] = []
    locked = 0
    for r in recipes if isinstance(recipes, list) else []:
        if not isinstance(r, dict):
            continue
        tier = r.get("tier", 0)
        tier = tier if isinstance(tier, int) else 0
        if tier > level:
            locked += 1
            continue
        buy, sell = _stack(r.get("buyA")), _stack(r.get("sell"))
        if buy is None or sell is None or len(out) >= MAX_TRADES:
            continue
        uses, max_uses = r.get("uses", 0), r.get("maxUses", 0)
        out.append(
            Trade(
                tier,
                buy,
                _stack(r.get("buyB")),
                sell,
                _enchants(r.get("sell")),
                uses if isinstance(uses, int) else 0,
                max_uses if isinstance(max_uses, int) else 0,
            )
        )
    return out, locked


def _coat(kind: str, d: dict[str, Any]) -> list[str]:
    defs = [x[1:] for x in d.get("definitions") or [] if isinstance(x, str) and x[:1] == "+"]
    words: list[str] = []
    for full in defs:
        name = full.removeprefix("minecraft:")
        if kind == "cat" and name.startswith("cat_") and name[4:] in _CAT_COATS:
            words.append(name[4:])
        elif name.startswith("base_"):
            words.append(name[5:].replace("darkbrown", "dark brown"))
    if kind == "parrot" and isinstance(d.get("Variant"), int):
        words.extend([_PARROT_COATS[d["Variant"]]] if d["Variant"] in _PARROT_COATS else [])
    return words


def parse(d: Any, dim: int) -> Mob | None:
    """One actor compound as a Mob, or None when it isn't one we keep."""
    if not isinstance(d, dict):
        return None
    kind = _short(d.get("identifier"))
    pos = d.get("Pos")
    if not kind or not isinstance(pos, list) or len(pos) != 3:
        return None
    if not all(isinstance(p, int | float) and math.isfinite(p) for p in pos):
        return None
    custom = d.get("CustomName")
    name = custom if isinstance(custom, str) else ""
    tamed = d.get("IsTamed") == 1
    if kind not in VILLAGER_KINDS and not name and not tamed:
        return None
    uid = d.get("UniqueID")
    owner = d.get("OwnerNew")
    mob = Mob(
        uid if isinstance(uid, int) else 0,
        kind,
        dim,
        math.floor(pos[0]),
        math.floor(pos[1]),
        math.floor(pos[2]),
        name,
        tamed,
        owner if tamed and isinstance(owner, int) and owner != -1 else None,
        d.get("Sitting") == 1,
        d.get("IsBaby") == 1,
        _coat(kind, d) if tamed else [],
    )
    if mob.villager:
        level = d.get("TradeTier", 0)
        mob.level = level if isinstance(level, int) and 0 <= level <= 4 else 0
        job = d.get("PreferredProfession")
        variant = d.get("Variant")
        if kind == "wandering_trader":
            mob.job = "wandering trader"
        elif isinstance(job, str) and job:
            mob.job = job
        else:
            mob.job = _JOBS.get(variant if isinstance(variant, int) else 0, "unemployed")
        xp = d.get("TradeExperience", 0)
        mob.locked_in = isinstance(xp, int) and xp > 0
        mob.trades, mob.locked_trades = _trades(d, mob.level)
    return mob


def _digp_place(key: bytes) -> tuple[int, int, int]:
    cx, cz = struct.unpack_from("<ii", key, 4)
    dim = struct.unpack_from("<i", key, 12)[0] if len(key) == 16 else 0
    return dim, cx, cz


def _is_legacy(key: bytes) -> bool:
    return len(key) in (9, 13) and key[-1] == LEGACY_ENTITY


def _legacy_dim(key: bytes) -> int:
    return struct.unpack_from("<i", key, 8)[0] if len(key) == 13 else 0


class EntityIndex:
    """The world's villagers, pets and named mobs, read from the actor records and kept
    until the files change (at most every containers.REREAD_S). It shares the container
    index's sweep (`containers.refresh_together`)."""

    def __init__(self, db: Path, clock=time.monotonic) -> None:
        self.db = db
        self._clock = clock
        self._read_at: float | None = None
        self._signature: tuple = ()
        self.mobs: list[Mob] = []
        self.records = 0  # candidate records parsed
        self.unreadable = 0
        self.unplaced = 0  # candidates no digp lists
        self.overflow = {"villagers": 0, "pets": 0, "named": 0}

    def wants(self, key: bytes) -> bool:
        if key.startswith(ACTOR_PREFIX):
            return len(key) == len(ACTOR_PREFIX) + 8
        if key.startswith(DIGP):
            return len(key) in (12, 16)
        return _is_legacy(key)

    def keeps(self, key: bytes, value: bytes) -> bool:
        return key.startswith(DIGP) or is_candidate(value)

    def refresh(self, force: bool = False) -> None:
        containers.refresh_together([self], force)

    def load(self, best: dict[bytes, bytes]) -> None:
        placed: dict[bytes, int] = {}
        for key, value in best.items():
            if key.startswith(DIGP):
                dim, _, _ = _digp_place(key)
                for i in range(0, len(value) - len(value) % 8, 8):
                    placed[value[i : i + 8]] = dim
        found: list[Mob] = []
        records = unreadable = unplaced = 0
        for key, value in best.items():
            if key.startswith(DIGP):
                continue
            records += 1
            try:
                if key.startswith(ACTOR_PREFIX):
                    dim = placed.get(key[len(ACTOR_PREFIX) :])
                    if dim is None:
                        unplaced += 1
                        continue
                    mob = parse(nbt.read(value)[1], dim)
                    found.extend([mob] if mob else [])
                else:
                    dim = _legacy_dim(key)
                    found.extend(m for d in nbt.read_all(value) if (m := parse(d, dim)))
            except (nbt.NbtError, struct.error, UnicodeDecodeError):
                unreadable += 1
        self._keep(found)
        self.records, self.unreadable, self.unplaced = records, unreadable, unplaced

    def _keep(self, found: list[Mob]) -> None:
        caps = {"villagers": MAX_VILLAGERS, "pets": MAX_PETS, "named": MAX_NAMED}
        held = dict.fromkeys(caps, 0)
        over = dict.fromkeys(caps, 0)
        kept: list[Mob] = []
        for mob in found:
            kinds = [
                k
                for k, yes in (("villagers", mob.villager), ("pets", mob.tamed), ("named", mob.name))
                if yes
            ]
            room = [k for k in kinds if held[k] < caps[k]]
            for k in kinds:
                if k in room:
                    held[k] += 1
                else:
                    over[k] += 1
            if room:
                kept.append(mob)
        self.mobs, self.overflow = kept, over

    def counts(self) -> dict[str, Any]:
        return {
            "villagers_indexed": sum(m.villager for m in self.mobs),
            "pets_indexed": sum(m.tamed for m in self.mobs),
            "named_indexed": sum(bool(m.name) for m in self.mobs),
            "records": self.records,
            "unreadable_records": self.unreadable,
            "unplaced_records": self.unplaced,
            "overflow": dict(self.overflow),
        }

    def villagers(
        self,
        dim: int,
        x: int,
        z: int,
        *,
        profession: str = "",
        sells: str = "",
        enchant: str = "",
        radius: int | None = None,
        limit: int = 5,
    ) -> tuple[list[dict[str, Any]], int]:
        """The nearest villagers that match, nearest first, each with the trades that
        matched (every trade it offers when no `sells`/`enchant` was asked), and how
        many matched in range."""
        hits: list[tuple[int, Mob, list[Trade]]] = []
        for m in self.mobs:
            if not m.villager or m.dim != dim:
                continue
            if profession and m.job.replace(" ", "_") != profession:
                continue
            trades = [
                t
                for t in m.trades
                if (not sells or t.sell[0] == sells)
                and (not enchant or any(e == enchant for e, _ in t.enchantments))
            ]
            if (sells or enchant) and not trades:
                continue
            distance = round(math.dist((x, z), (m.x, m.z)))
            if radius is not None and distance > radius:
                continue
            hits.append((distance, m, trades))
        hits.sort(key=lambda h: h[0])
        return [_villager_out(d, m, t) for d, m, t in hits[:limit]], len(hits)

    def pets(
        self,
        dim: int,
        x: int,
        z: int,
        *,
        owner: int | None = None,
        kind: str = "",
        name: str = "",
        radius: int | None = None,
        limit: int = 5,
        everywhere: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Tamed animals (only `owner`'s when given), or with `name` any mob of that
        name, `owner`'s first; nearest first in `dim`, then (with `everywhere`) those in
        the other dimensions. How many matched in range."""
        want = name.casefold().strip()
        if want:
            # An exact name wins; only when none has it does a part of a name match.
            named = [m for m in self.mobs if m.name]
            pool = [m for m in named if m.name.casefold() == want] or [
                m for m in named if want in m.name.casefold()
            ]
        else:
            pool = [m for m in self.mobs if m.tamed and (owner is None or m.owner == owner)]
        hits: list[tuple[tuple[int, int, int], int | None, Mob]] = []
        for m in pool:
            if kind and m.kind != kind:
                continue
            if m.dim != dim and not everywhere:
                continue
            distance = round(math.dist((x, z), (m.x, m.z))) if m.dim == dim else None
            if radius is not None and (distance is None or distance > radius):
                continue
            mine = 0 if owner is not None and m.owner == owner else 1
            hits.append(((mine, 0 if distance is not None else 1, distance or 0), distance, m))
        hits.sort(key=lambda h: h[0])
        return [_pet_out(d, m) for _, d, m in hits[:limit]], len(hits)


def _villager_out(distance: int, m: Mob, trades: list[Trade]) -> dict[str, Any]:
    return {
        "kind": m.kind,
        "job": m.job,
        "level": m.level,
        "level_name": LEVELS[m.level],
        "locked_in": m.locked_in,
        "name": m.name,
        "x": m.x,
        "y": m.y,
        "z": m.z,
        "distance": distance,
        "trades": [
            {
                "tier": t.tier,
                "buy": list(t.buy),
                "buy2": list(t.buy2) if t.buy2 else None,
                "sell": list(t.sell),
                "enchantments": [list(e) for e in t.enchantments],
                "uses": t.uses,
                "max_uses": t.max_uses,
                "used_up": t.max_uses > 0 and t.uses >= t.max_uses,
            }
            for t in trades
        ],
        "locked_trades": m.locked_trades,
    }


def _pet_out(distance: int | None, m: Mob) -> dict[str, Any]:
    return {
        "kind": m.kind,
        "name": m.name,
        "id": str(m.uid),
        "tamed": m.tamed,
        "owner": None if m.owner is None else str(m.owner),
        "sitting": m.sitting,
        "baby": m.baby,
        "coat": m.coat,
        "dim": m.dim,
        "x": m.x,
        "y": m.y,
        "z": m.z,
        "distance": distance,
    }


def portals(db: Path) -> list[dict[str, Any]]:
    """Every portal the world recorded: {dim, x, y, z, span, xa, za}. The record can
    outlive a broken portal; the caller checks the block (§M4e)."""
    raw = leveldb.read_keys(db, {PORTALS}).get(PORTALS)
    if not raw:
        return []
    try:
        top = nbt.read(raw)[1]
    except (nbt.NbtError, struct.error, UnicodeDecodeError):
        return []
    data = top.get("data") if isinstance(top, dict) else None
    records = data.get("PortalRecords") if isinstance(data, dict) else None
    out: list[dict[str, Any]] = []
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        got: dict[str, Any] = {
            k: r.get(k) for k in ("DimId", "TpX", "TpY", "TpZ", "Span", "Xa", "Za")
        }
        if not all(isinstance(v, int) for v in got.values()):
            continue
        out.append(
            {
                "dim": got["DimId"],
                "x": got["TpX"],
                "y": got["TpY"],
                "z": got["TpZ"],
                "span": got["Span"],
                "xa": got["Xa"],
                "za": got["Za"],
            }
        )
    return out
