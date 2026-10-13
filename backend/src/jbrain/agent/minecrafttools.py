"""Minecraft_Dave's tools (docs/plans/MINECRAFT_BEDROCK_PLAN.md §P1).

Everything is about ONE player at a time — the chat's player (`mc_chat_player`), which
starts as the owner's gamertag setting and changes only through `mc_player`. Goals, the
progress log and memory are that player's; the server and world tools read the box's
Bedrock server through its sidecar.

**Writes run without approval, but only with a player defined** (owner decision): a chat
with no player refuses them and says how to set one, rather than guessing whose data to
touch. Memory has no whole-document write at all — add, replace one line, remove one line
— and a replaced or removed line is kept as history (`superseded_at`), never deleted.

Gamertags, world names and player-typed log lines are player-authored, so they reach
the model inside the untrusted-data fence the persona prompt declares inert.

`web`-classed like the archivist's memory: only an allowlist that names them reaches
them, so curator's wildcard never absorbs a Minecraft tool.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from fastapi import HTTPException
from sqlalchemy import text

from jbrain.agent.briefs import FEED_TAG, neutralize_boundary
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft import livescan, scans, travel
from jbrain.minecraft import sessions as mc_sessions
from jbrain.minecraft import targets as tricorder
from jbrain.minecraft.asker import Asker, Spot, asker_for
from jbrain.minecraft.sessions import check_gamertag

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger(__name__)


# Memory caps (plan §P1): a working set of facts, not a diary — the log is the diary.
MEMORY_MAX_LINES = 60
MEMORY_MAX_CHARS = 6000
MEMORY_LINE_MAX = 300
LOG_MAX = 500
GOAL_TITLE_MAX = 120
# A goal's item count (§M24): a stack of 64 up to well past any build anyone finishes.
GOAL_COUNT_MAX = 100_000
PLACE_NAME_MAX = 40
# Per player and world: a handful of bases, farms and portals, not a survey.
PLACES_MAX = 100
# From the PWA a place is saved at the player's position only while it's this fresh:
# the sidecar samples every 10 s and the drain lands them within another 10.
PLACE_FRESH_S = 60

# Bedrock structure and biome ids: lowercase words, optionally namespaced.
_GAME_ID = re.compile(r"^(minecraft:)?[a-z_]{2,40}$")
_GOAL_STATUSES = ("open", "done", "abandoned")
_COORD_MAX = 30_000_000
_DIMENSIONS = ("overworld", "nether", "the_end")
_DIMENSION_NAMES = {"overworld": "Overworld", "nether": "Nether", "the_end": "End"}
# `locate`'s answer when it found one ("The nearest mansion is at block 2312, ~, -1544");
# anything else (an unknown id, a down generator) searched nothing and costs no scan.
_LOCATED = re.compile(r"\bis at block\b")


class Found(ToolOutput):
    """A tool's answer plus the facts behind its text, for the in-game door's fast path
    (§M18), which fills Dave's fixed lines from these rather than reading prose meant for
    the model. The model sees only the text; `found` never reaches it."""

    found: dict[str, Any]

    def __new__(cls, content: str, brief: str, **found: Any) -> Found:
        out = cast("Found", ToolOutput.__new__(cls, content, result_brief=brief))
        out.found = found
        return out


# A search's first hit as the tool texts print it: `locate`'s console line (its y is
# "~" or "(y?)" when the generator doesn't say) and mc_find_block's nearest-first list.
# Read off the text, not `Found`, because a free repeat (§M17) answers from the stored text.
_LOCATE_HIT = re.compile(r"\bis at block (-?\d+), (?:~|\(y\?\)|(-?\d+)), (-?\d+)")
_BLOCK_HIT = re.compile(r"\bat x=(-?\d+) y=(-?\d+) z=(-?\d+), ")
# The pack's answer to a look around for items on the ground (rules.js `itemsLines`).
_GROUND_HIT = re.compile(
    r"\bitems ground: (?P<n>\d+) (?P<type>[a-z0-9_:]+) at x=(?P<x>-?\d+) y=(?P<y>-?\d+)"
    r" z=(?P<z>-?\d+), (?P<blocks>\d+) blocks from .+? \((?P<dim>overworld|nether|the_end)\)"
)
# An item id: namespace optional (the box's own are `jbrain:`), digits allowed (music discs).
_ITEM_ID = re.compile(r"^([a-z0-9_]{1,32}:)?[a-z0-9_]{2,64}$")
# The Tricorder's short-range scan (lore, canon 2026-10-12): about 3 chunks, free in game.
NEAR_BLOCKS = 48
# The pack's answer to a count of what a player carries (rules.js `carryLine`).
_CARRY_HIT = re.compile(
    r"\bcarry counted for .+? in (?P<dim>overworld|nether|the_end) at x=(?P<x>-?\d+)"
    r" y=(?P<y>-?\d+) z=(?P<z>-?\d+): (?P<counts>[a-z0-9_:]+=\d+(?: [a-z0-9_:]+=\d+)*);"
    r" shulker boxes=(?P<boxes>\d+)"
)

# The pack's answer to a scan it took: `scan set for <player>: <label> (<ids>)`.
_SCAN_SET = re.compile(r"\bscan set for .+?: (?P<label>[a-z0-9 .]+) \((?P<types>[a-z0-9_:., ]+)\)")
# The pack's answer to a mob search (rules.js `findLine`).
_MOB_HIT = re.compile(
    r"\bfind found (?P<type>[a-z_:]+) at x=(?P<x>-?\d+) y=(?P<y>-?\d+) z=(?P<z>-?\d+),"
    r" (?P<blocks>\d+) blocks from .+ \((?P<dim>overworld|nether|the_end)\)"
)


def first_hit(result: str, dim: str) -> Spot | None:
    """Where the first thing a deep scan found is, or None when it found nothing."""
    if m := _BLOCK_HIT.search(result):
        return Spot(int(m.group(1)), int(m.group(2)), int(m.group(3)), dim)
    if m := _LOCATE_HIT.search(result):
        y = None if m.group(2) is None else int(m.group(2))
        return Spot(int(m.group(1)), y, int(m.group(3)), dim)
    return None


def as_of(age_s: Any) -> str:
    """The lore's "…as of a few minutes ago" with the number filled in, for files two
    minutes old or more; "" when they're fresher (or the age isn't known)."""
    if not isinstance(age_s, int | float) or age_s < 120:
        return ""
    minutes = int(age_s // 60)
    if minutes < 120:
        return f" (as of {minutes} minutes ago)"
    hours = minutes // 60
    return f" (as of {hours} hours ago)" if hours < 48 else f" (as of {hours // 24} days ago)"


def _item_words(name: str) -> str:
    return name.removeprefix("minecraft:").replace("_", " ")


def item_id(raw: Any) -> str:
    """A model-typed item as a goal stores it — lowercase, spaces as underscores, the
    `minecraft:` namespace dropped (others kept) — or "" when it isn't an item id."""
    name = str(raw or "").strip().lower().replace(" ", "_")
    return name.removeprefix("minecraft:") if _ITEM_ID.fullmatch(name) else ""


def full_id(item: str) -> str:
    """An item id as the world and the pack name it: namespaced."""
    return item if ":" in item else f"minecraft:{item}"


def goal_line(goal: Any) -> str:
    """A goal's title, with what it counts when it counts something."""
    if goal.item is None:
        return str(goal.title)
    return f"{goal.title} ({goal.item} ×{goal.item_count})"


def tally(need: int, carried: int | None, chests: list[tuple[str, int]]) -> tuple[int, str, str]:
    """A goal count's total, its headline ("17 of 23 — 6 to go") and where they are
    ("12 in chests near home, 5 on you"; "" when there's none anywhere). `carried` is
    None when the pockets weren't counted."""
    have = (carried or 0) + sum(n for _, n in chests)
    head = (
        f"{have} of {need} — that's the goal reached"
        if have >= need
        else f"{have} of {need} — {need - have} to go"
    )
    parts = [f"{n} in chests near {label}" for label, n in chests if n]
    if carried:
        parts.append(f"{carried} on you")
    return have, head, ", ".join(parts)


def check_goal_item(raw_item: Any, raw_count: Any) -> tuple[str | None, int | None] | str:
    """The item and count a goal write passed (None for one left out), or why one of
    them can't be stored — checked before any write."""
    item = None
    if raw_item not in (None, ""):
        item = item_id(raw_item)
        if not item:
            return 'item is one Bedrock item id, e.g. "iron_block", "oak_log" or "diamond".'
    count = None
    if raw_count not in (None, ""):
        whole = not isinstance(raw_count, bool) and (
            not isinstance(raw_count, float) or raw_count.is_integer()
        )
        try:
            count = int(raw_count) if whole else None
        except (TypeError, ValueError):
            count = None
        if count is None or not 1 <= count <= GOAL_COUNT_MAX:
            return f"count is a whole number of items, 1 to {GOAL_COUNT_MAX:,}."
    return item, count


class Pocket:
    """What one online player carries of the asked items, and where they stand."""

    __slots__ = ("counts", "spot", "boxes")

    def __init__(self, counts: dict[str, int], spot: Spot, boxes: int) -> None:
        self.counts, self.spot, self.boxes = counts, spot, boxes


def fence(body: str) -> str:
    """Wrap player-authored text in the data boundary the persona prompt declares inert,
    defanging any boundary tag inside it first so it cannot close the envelope."""
    return f'<{FEED_TAG} source="minecraft-players">\n{neutralize_boundary(body)}\n</{FEED_TAG}>'


def name_key(gamertag: str) -> str:
    # The fallback key `sessions.player_key` uses, case-folded so "Steve42" and "steve42"
    # are one player until the server reports their xuid.
    return f"name:{gamertag.casefold()}"


def check_memory_op(current: list[str], op: str, line: int, new_text: str) -> str:
    """Why this memory edit can't be applied, or "" — pure, checked before any write."""
    if op not in ("add", "replace", "remove"):
        return f"'{op}' isn't a memory edit — use add, replace or remove."
    if op in ("add", "replace"):
        if not new_text:
            return "text is required — the fact to remember."
        if len(new_text) > MEMORY_LINE_MAX:
            return (
                f"that line is {len(new_text)} characters, over the {MEMORY_LINE_MAX} limit."
                " One fact per line — shorten it or split it."
            )
    if op in ("replace", "remove") and not 1 <= line <= len(current):
        return (
            f"there is no line {line} — this player's memory has {len(current)} line(s)."
            if current
            else "this player's memory is empty, so there is nothing to change. Use add."
        )
    after = list(current)
    if op == "add":
        if new_text.casefold() in (c.casefold() for c in current):
            return "that's already remembered."
        after.append(new_text)
    elif op == "replace":
        after[line - 1] = new_text
    else:
        after.pop(line - 1)
    if len(after) > MEMORY_MAX_LINES:
        return (
            f"that would make {len(after)} lines, over the {MEMORY_MAX_LINES} limit."
            " Consolidate: replace or remove a line instead of adding one."
        )
    if sum(len(a) for a in after) > MEMORY_MAX_CHARS:
        return (
            f"that would put this player's memory over {MEMORY_MAX_CHARS} characters."
            " Consolidate older lines first."
        )
    return ""


# A pack event line (`[jbrain] {"ev": …}`): the wrapper's to queue, never part of a reply.
_EVENT_LINE = re.compile(r"\[jbrain\] \{.*\}\s*$")


def _reply(lines: list[str]) -> str:
    """A console reply's text, its log-line prefixes and the pack's event lines dropped:
    a gate's structured line names its place, which could read as a gate number."""
    kept = [line for line in lines if not _EVENT_LINE.search(line)]
    return " ".join(line.split("] ", 1)[-1] for line in kept).strip() or "(no answer)"


# `/map/stock` and `jb:carry` each take at most this many item ids in one ask.
ITEMS_PER_ASK = 8
Sidecar = Callable[..., Awaitable[dict[str, Any] | str]]


async def carry_pocket(sidecar: Sidecar, gamertag: str, names: list[str]) -> Pocket | str:
    """What an online player carries of `names` (inventory, armor, off-hand), from the
    game itself (the pack's `jb:carry`), or a sentence saying why it wasn't counted.
    Past ITEMS_PER_ASK names it asks again, and the answers are merged."""
    pocket: Pocket | None = None
    for start in range(0, len(names), ITEMS_PER_ASK):
        query = {"player": gamertag, "types": names[start : start + ITEMS_PER_ASK]}
        got = await sidecar(
            "POST",
            "/command",
            json={"command": f"scriptevent jb:carry {json.dumps(query)}", "wait_s": 3.0},
        )
        if isinstance(got, str):
            return got
        reply = _reply([str(line) for line in got.get("lines", [])])
        m = _CARRY_HIT.search(reply)
        if m is None:
            if "carry refused" in reply:
                return f"What they carry wasn't counted: {fence(reply)}"
            return (
                "What they carry wasn't counted: the jbrain pack didn't answer (it updates"
                " when the server restarts)."
            )
        counts = {k: int(n) for k, n in (pair.split("=") for pair in m["counts"].split())}
        spot = Spot(int(m["x"]), int(m["y"]), int(m["z"]), m["dim"])
        if pocket is None:
            pocket = Pocket(counts, spot, int(m["boxes"]))
        else:
            pocket.counts.update(counts)
    return pocket if pocket is not None else "What they carry wasn't counted: no item named."


class Stock:
    """What `stock_near` found: per centre, each item's total; the files' age and time
    (None when unknown); and the sidecar's word on the flush ("flushed", "recent", …)."""

    __slots__ = ("per", "age_s", "as_of", "flush")

    def __init__(
        self, per: list[dict[str, int]], age_s: float | None, as_of: float | None, flush: str
    ) -> None:
        self.per, self.age_s, self.as_of, self.flush = per, age_s, as_of, flush


async def stock_near(
    sidecar: Sidecar, slot: str, centres: list[Spot], items: list[str], *, flush: bool
) -> Stock | str:
    """How many of each of `items` (full ids) the containers within NEAR_BLOCKS of each
    centre hold in one world slot's saved files, each container once, toward its nearest
    centre (the sidecar's `/map/stock`: one ask a dimension, per ITEMS_PER_ASK items). A
    slot that isn't loaded is read just the same; only the flush needs it running. A
    sentence when the sidecar couldn't count."""
    per: list[dict[str, int]] = [{} for _ in centres]
    age: float | None = None
    when: float | None = None
    flushed = "not asked"
    for dim in _DIMENSIONS:
        here = [i for i, sp in enumerate(centres) if sp.dim == dim]
        if not here:
            continue
        for start in range(0, len(items), ITEMS_PER_ASK):
            names = items[start : start + ITEMS_PER_ASK]
            got = await sidecar(
                "GET",
                "/map/stock",
                params={
                    "slot": slot,
                    "dim": dim,
                    "centres": ";".join(f"{centres[i].x},{centres[i].z}" for i in here),
                    "items": ",".join(names),
                    "radius": NEAR_BLOCKS,
                    "flush": 1 if flush else 0,
                },
            )
            if isinstance(got, str):
                return got
            for i, c in zip(here, got.get("centres") or [], strict=False):
                totals = c.get("totals") or {}
                for n in names:
                    per[i][n] = per[i].get(n, 0) + int(totals.get(n, 0))
            if isinstance(got.get("age_s"), int | float):
                age = max(age or 0, float(got["age_s"]))
            if isinstance(got.get("as_of"), int | float):
                when = float(got["as_of"]) if when is None else min(when, float(got["as_of"]))
            flushed = str(got.get("flush") or flushed)
    return Stock(per, age, when, flushed)


def _int(raw: Any, default: int, low: int, high: int) -> int | None:
    """A model-supplied count clamped to range; None when it isn't a number at all."""
    if raw in (None, ""):
        return default
    try:
        return max(low, min(int(raw), high))
    except (TypeError, ValueError):
        return None


def _when(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


_COMPASS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)


def bearing(fx: float, fz: float, tx: float, tz: float) -> tuple[str, int]:
    """The compass direction and distance across from one point to another. North is -z
    in Minecraft, east +x."""
    dx, dz = tx - fx, tz - fz
    degrees = math.degrees(math.atan2(dx, -dz)) % 360
    return _COMPASS[round(degrees / 45) % 8], round(math.hypot(dx, dz))


def spot_text(spot: Spot) -> str:
    y = f", y {spot.y}" if spot.y is not None else ""
    return f"({spot.x}, {spot.z}{y}) in the {_DIMENSION_NAMES[spot.dim]}"


def from_asker(asker: Asker | None, spot: Spot) -> str:
    """How to get to `spot` from where the in-game asker stands, or "" outside the door."""
    if asker is None:
        return ""
    if asker.at.dim != spot.dim:
        there, here = _DIMENSION_NAMES[spot.dim], _DIMENSION_NAMES[asker.at.dim]
        return f" It's in the {there}; you're in the {here}."
    way, far = bearing(asker.at.x, asker.at.z, spot.x, spot.z)
    return " You're standing there." if far < 3 else f" From you: {far} blocks {way}."


def ago_words(seconds: float) -> str:
    """How long ago, in the card's own words: "40 s ago", "3 min ago", "2 h ago"."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def depth_words(dy: int) -> str:
    """A found block's height against the player's: what a top-down map can't show."""
    if dy == 0:
        return "level with them"
    n = abs(dy)
    return f"{n} block{'s' if n != 1 else ''} {'below' if dy < 0 else 'above'} them"


_SCAN_LABEL = re.compile(r"[^a-z0-9 .]")


def scan_words(
    gamertag: str,
    scan: dict[str, Any],
    here: dict[str, Any] | None,
    online: bool | None,
    now: float,
) -> str:
    """Dave's read of one player's live scan (§M25): what it scans for, how far it reaches
    (16 blocks across, and 16 up and down cut at the world's floor and ceiling, from where
    they stand), and what the last scan found, with the distance the held Tricorder shows
    (across, to the block's centre) and the depth it can't. `here` is where they are (dim,
    x, y, z), None when unknown; `online` None when the server can't say."""
    label = _SCAN_LABEL.sub("", str(scan.get("label") or "").lower()).strip() or "a block"
    who = fence(gamertag)
    head = f"{who}'s Tricorder is scanning for {label}."
    if online is False:
        return (
            f"{head} They're offline: the Tricorder remembers the scan and picks it up again"
            " when they're back on. Nothing is scanned while they're away."
        )
    lines = [head]
    if online is None:
        lines.append("The server can't say who's on right now, so this is the last report.")
    if here is not None:
        lo, hi = livescan.y_range(str(here["dim"]), float(here["y"]))
        lines.append(
            f"Every 10 seconds it finds the nearest one within {livescan.REACH} blocks of them:"
            f" across, and from y {lo} to {hi} up and down (they're at y"
            f" {math.floor(float(here['y']))}; the box stops at the world's floor and ceiling)."
        )
    else:
        lines.append(
            f"Every 10 seconds it finds the nearest one within {livescan.REACH} blocks of them,"
            " across and up and down."
        )
    at = scan.get("at")
    when = f"The last scan, {ago_words(now - float(at))}," if at is not None else "The last scan"
    found = scan.get("found")
    if scan.get("state") == "set" or at is None:
        lines.append("It hasn't reported a scan yet; the first comes within 10 seconds.")
    elif found is None:
        lines.append(f"{when} found none in that range.")
    elif here is not None and here["dim"] == found["dim"]:
        way, far = bearing(float(here["x"]), float(here["z"]), found["x"] + 0.5, found["z"] + 0.5)
        across = (
            "right where they stand"
            if far < 1
            else f"{far} block{'s' if far != 1 else ''} {way} of them"
        )
        dy = int(found["y"]) - math.floor(float(here["y"]))
        lines.append(
            f"{when} found one {across}, at y {found['y']}: {depth_words(dy)}"
            f" (X {found['x']}, Z {found['z']})."
        )
    else:
        lines.append(
            f"{when} found one at X {found['x']}, Z {found['z']}, y {found['y']} in the"
            f" {_DIMENSION_NAMES.get(found['dim'], found['dim'])}."
        )
    return " ".join(lines)


_ROMAN = ("I", "II", "III", "IV", "V")
# What players call a villager's job or a pet, to the word the world's files use.
_PET_KINDS = {"dog": "wolf", "puppy": "wolf", "kitten": "cat", "kitty": "cat", "pony": "horse"}
_WORD_ID = re.compile(r"^[a-z0-9_]{2,40}$")
# The Overworld and the Nether are joined by portals; a target across them gets the
# known portals on the far side (§M4e), so the Tricorder can point at one.
_PORTAL_SIDES = {"overworld": "nether", "nether": "overworld"}
VIA_MAX = 8


def _a(word: str) -> str:
    return f"an {word}" if word[:1].lower() in "aeiou" else f"a {word}"


def _stack_words(stack: list[Any]) -> str:
    name, n = str(stack[0]), int(stack[1])
    return f"{n} {_item_words(name)}"


def _enchant_words(name: str) -> str:
    """An enchantment as the game shows it: "Mending", "Luck of the Sea"."""
    words = _item_words(name).split()
    return " ".join(w if i and w in ("of", "the") else w.capitalize() for i, w in enumerate(words))


def trade_text(t: dict[str, Any]) -> str:
    """One trade as Dave would say it: "an enchanted book (Mending I) for 12 emerald and
    1 book"; a used-up one says it waits for a restock."""
    sell = t["sell"]
    what = _item_words(str(sell[0]))
    what = _a(what) if int(sell[1]) == 1 else f"{sell[1]} {what}"
    ench = t.get("enchantments") or []
    if ench:
        what += (
            " ("
            + ", ".join(
                f"{_enchant_words(str(e))} {_ROMAN[lvl - 1] if 1 <= lvl <= 5 else lvl}"
                for e, lvl in ench
            )
            + ")"
        )
    price = _stack_words(t["buy"]) + (f" and {_stack_words(t['buy2'])}" if t.get("buy2") else "")
    used = " — used up until it restocks" if t.get("used_up") else ""
    return f"{what} for {price}{used}"


def pet_kind(raw: Any) -> str:
    word = str(raw or "").strip().lower().replace(" ", "_").removeprefix("minecraft:")
    return _PET_KINDS.get(word, word)


def place_name(raw: Any) -> str:
    """A place's name as stored: printable, spacing collapsed; "" when it can't be one."""
    name = " ".join("".join(c for c in str(raw or "") if c.isprintable()).split())
    return name if 0 < len(name) <= PLACE_NAME_MAX else ""


async def _places(s: AsyncSession, key: str, world: str) -> list[Any]:
    rows = await s.execute(
        text(
            "SELECT id, name, x, y, z, dim FROM app.mc_places"
            " WHERE xuid = :k AND world = :w ORDER BY lower(name)"
        ),
        {"k": key, "w": world},
    )
    return list(rows)


def _spot(row: Any) -> Spot:
    return Spot(int(row.x), None if row.y is None else int(row.y), int(row.z), str(row.dim))


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


class Player:
    __slots__ = ("key", "gamertag")

    def __init__(self, key: str, gamertag: str) -> None:
        self.key, self.gamertag = key, gamertag


async def resolve_player(s: AsyncSession, gamertag: str) -> Player:
    """A gamertag → the key its data lives under. A player the server has seen is keyed
    by xuid; one it hasn't is keyed by name until it has, and the first time an xuid is
    known any name-keyed goals, log or memory move onto it, so nothing written before a
    player's first join is stranded."""
    row = (
        await s.execute(
            text(
                "SELECT xuid, gamertag FROM app.mc_player_sessions"
                " WHERE lower(gamertag) = lower(:g) AND xuid NOT LIKE 'name:%'"
                " ORDER BY started_at DESC LIMIT 1"
            ),
            {"g": gamertag},
        )
    ).first()
    if row is None:
        return Player(name_key(gamertag), gamertag)
    key = {"x": row.xuid, "n": name_key(gamertag)}
    for table in ("mc_goals", "mc_goal_log", "mc_chat_player"):
        await s.execute(text(f"UPDATE app.{table} SET xuid = :x WHERE xuid = :n"), key)
    # A place the account already has under that name wins over the one kept by name.
    await s.execute(
        text(
            "DELETE FROM app.mc_places n WHERE n.xuid = :n AND EXISTS (SELECT 1 FROM"
            " app.mc_places x WHERE x.xuid = :x AND x.world = n.world"
            " AND lower(x.name) = lower(n.name))"
        ),
        key,
    )
    await s.execute(text("UPDATE app.mc_places SET xuid = :x WHERE xuid = :n"), key)
    # Name-keyed memory lines go after any the xuid already holds, so positions can't clash.
    await s.execute(
        text(
            "UPDATE app.mc_player_memory SET xuid = :x, seq = seq + (SELECT coalesce(max(seq), 0)"
            " FROM app.mc_player_memory WHERE xuid = :x) WHERE xuid = :n"
        ),
        key,
    )
    return Player(row.xuid, row.gamertag)


async def _current_lines(s: AsyncSession, key: str) -> list[tuple[int, str]]:
    rows = await s.execute(
        text(
            "SELECT seq, text FROM app.mc_player_memory"
            " WHERE xuid = :k AND superseded_at IS NULL ORDER BY seq"
        ),
        {"k": key},
    )
    return [(r.seq, r.text) for r in rows]


async def _goals(s: AsyncSession, key: str) -> list[Any]:
    # Numbered by creation over ALL of a player's goals, so "goal 3" never moves when an
    # earlier one is finished.
    rows = await s.execute(
        text(
            "SELECT id, title, notes, status, world, created_at, finished_at, item,"
            " item_count FROM app.mc_goals WHERE xuid = :k ORDER BY created_at, id"
        ),
        {"k": key},
    )
    return list(rows)


async def player_context(s: AsyncSession, key: str) -> str:
    """The block a Minecraft_Dave chat starts with: the player's memory and open goals,
    numbered the way the tools address them. Empty when there is nothing yet."""
    lines = await _current_lines(s, key)
    goals = [(i, g) for i, g in enumerate(await _goals(s, key), start=1) if g.status == "open"]
    parts: list[str] = []
    if lines:
        parts.append(
            "Memory:\n" + "\n".join(f"{i}. {t}" for i, (_, t) in enumerate(lines, start=1))
        )
    if goals:
        parts.append("Open goals:\n" + "\n".join(f"{i}. {goal_line(g)}" for i, g in goals))
    return fence("\n\n".join(parts)) if parts else ""


async def bind_chat(s: AsyncSession, agent_session_id: str | None, player: Player) -> None:
    if not agent_session_id:
        return
    # Binding to a different player drops the chat's intro snapshot, so the next turn
    # starts from that player's memory and goals.
    await s.execute(
        text(
            "INSERT INTO app.mc_chat_player (agent_session_id, xuid, gamertag)"
            " VALUES (:sid, :k, :g) ON CONFLICT (agent_session_id) DO UPDATE"
            " SET xuid = excluded.xuid, gamertag = excluded.gamertag, set_at = now(),"
            " intro = CASE WHEN app.mc_chat_player.xuid = excluded.xuid"
            " THEN app.mc_chat_player.intro END"
        ),
        {"sid": agent_session_id, "k": player.key, "g": player.gamertag},
    )


async def chat_player_for(
    s: AsyncSession,
    settings_store: SqlSettingsStore | None,
    session_ctx: SessionContext,
    agent_session_id: str | None,
) -> Player | None:
    """A chat's player: the one bound to it, else the owner's gamertag — bound on first
    use, so changing the setting later doesn't swap the player under an open chat."""
    if agent_session_id:
        row = (
            await s.execute(
                text("SELECT xuid, gamertag FROM app.mc_chat_player WHERE agent_session_id = :sid"),
                {"sid": agent_session_id},
            )
        ).first()
        if row is not None:
            # A chat bound to an xuid stays with that account: a gamertag can later belong
            # to someone else, and re-resolving by name would swap players mid-chat. Only
            # a name-keyed binding (not seen on the server yet) is looked up again.
            if not row.xuid.startswith("name:"):
                return Player(row.xuid, row.gamertag)
            return await resolve_player(s, row.gamertag)
    raw = await settings_store.minecraft_gamertag(session_ctx) if settings_store else None
    tag = check_gamertag(raw) if raw else None
    if tag is None:
        return None
    player = await resolve_player(s, tag)
    await bind_chat(s, agent_session_id, player)
    return player


async def chat_intro(
    maker: async_sessionmaker[AsyncSession],
    settings_store: SqlSettingsStore | None,
    session_ctx: SessionContext,
    agent_session_id: str,
) -> str:
    """What a Minecraft_Dave turn's system prompt ends with: whose chat it is, and that
    player's memory and open goals as they were when the chat started (plan §P1, "read at
    the start of a chat").

    Snapshotted on first use and reused for the chat's life, so the system prompt stays
    byte-identical turn to turn and the local engine's prefix cache holds. Edits made
    during the chat reach the model through its own tool results; switching players
    takes a fresh snapshot."""
    async with scoped_session(maker, session_ctx) as s:
        player = await chat_player_for(s, settings_store, session_ctx, agent_session_id)
        if player is None:
            return (
                "## This chat\nNo player is set for this chat and the owner hasn't set a"
                " gamertag. Before writing any goal, log entry or memory, ask who it's for"
                " and call mc_player."
            )
        saved = (
            await s.execute(
                text("SELECT intro FROM app.mc_chat_player WHERE agent_session_id = :sid"),
                {"sid": agent_session_id},
            )
        ).scalar()
        if saved:
            return str(saved)
        context = await player_context(s, player.key)
        about = f"## This chat\nThis chat is about the player {fence(player.gamertag)}."
        intro = f"{about}\n\n{context}" if context else f"{about} Nothing is remembered yet."
        await s.execute(
            text("UPDATE app.mc_chat_player SET intro = :i WHERE agent_session_id = :sid"),
            {"i": intro, "sid": agent_session_id},
        )
    return intro


def build_minecraft_handlers(
    maker: async_sessionmaker[AsyncSession],
    config: Any,
    settings_store: SqlSettingsStore | None,
    *,
    scan_book: Callable[[SessionContext], scans.ScanBook] | None = None,
    target_book: Callable[[SessionContext], tricorder.TargetBook] | None = None,
) -> dict[str, ToolHandler]:
    """Minecraft_Dave's tools, bound to the app's sessionmaker, its config (the sidecar's
    address and token) and the settings store (the owner's gamertag). Each DB handler runs
    under `ctx.session`, so the owner-only RLS on the Minecraft tables is the gate.
    `scan_book` replaces the deep-scan ledger and `target_book` the Tricorder targets the
    map draws (unit tests keep both in memory)."""

    def book_for(ctx: ToolContext) -> scans.ScanBook:
        if scan_book is not None:
            return scan_book(ctx.session)
        return scans.SqlScanBook(maker, ctx.session)

    def targets_for(ctx: ToolContext) -> tricorder.TargetBook:
        if target_book is not None:
            return target_book(ctx.session)
        return tricorder.SqlTargetBook(maker, ctx.session)

    async def chat_player(s: AsyncSession, ctx: ToolContext) -> Player | None:
        # In game the player is whoever the server says typed the command, whatever the
        # chat's binding or the owner's setting says.
        asker = asker_for(ctx.agent_session_id)
        if asker is not None:
            return Player(asker.key, asker.gamertag)
        return await chat_player_for(s, settings_store, ctx.session, ctx.agent_session_id)

    async def bind(s: AsyncSession, ctx: ToolContext, player: Player) -> None:
        await bind_chat(s, ctx.agent_session_id, player)

    only_you = "In game, Dave only works with the player asking: nothing was done for anyone else."

    no_player = (
        "This chat has no Minecraft player yet, so nothing was written. Ask which player"
        " it's for and call mc_player with their gamertag — or the owner can set theirs on"
        " the Minecraft screen."
    )

    async def sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        """The sidecar's answer, or a sentence saying why there isn't one."""
        try:
            return await mc.call(config, method, path, **kw)
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"

    async def console(command: str) -> list[str] | str:
        got = await sidecar("POST", "/command", json={"command": command, "wait_s": 3.0})
        if isinstance(got, str):
            return got
        return [str(line) for line in got.get("lines", [])]

    # --- the player ------------------------------------------------------------------

    async def mc_player(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        raw = str(arguments.get("gamertag") or "").strip()
        asker = asker_for(ctx.agent_session_id)
        if asker is not None and raw and raw.casefold() != asker.gamertag.casefold():
            return ToolOutput(only_you, result_brief="refused")
        async with scoped_session(maker, ctx.session) as s:
            if not raw or asker is not None:
                player = await chat_player(s, ctx)
                if player is None:
                    return ToolOutput(no_player, result_brief="no player")
                return ToolOutput(
                    "This chat is about " + fence(player.gamertag), result_brief=player.gamertag
                )
            tag = check_gamertag(raw)
            if tag is None:
                return "That isn't a gamertag (letters, digits and spaces, up to 16)."
            player = await resolve_player(s, tag)
            await bind(s, ctx, player)
            seen = not player.key.startswith("name:")
        note = "" if seen else " They haven't joined this server yet; their data is kept by name."
        return ToolOutput(
            f"This chat is now about {fence(player.gamertag)}.{note}",
            result_brief=f"→ {player.gamertag}",
        )

    # --- server, players, history -----------------------------------------------------

    async def mc_server_status(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        status = await sidecar("GET", "/status")
        if isinstance(status, str):
            return status
        lines = [
            f"Server: {status.get('state')}, Bedrock {status.get('version') or '?'}",
            f"LAN address: {status.get('lan_ip')}:19132",
        ]
        if status.get("uptime_s"):
            lines.append(f"Up for {_duration(float(status['uptime_s']))}")
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, dict):
            for w in worlds.get("slots", []):
                if w.get("exists"):
                    mark = " (loaded)" if w.get("active") else ""
                    lines.append(
                        f"World {w['id']}: {fence(str(w.get('name')))}{mark},"
                        f" {w.get('gamemode')}, {w.get('difficulty')}"
                    )
        online = [p.get("name", "") for p in status.get("players", [])]
        lines.append("Online: " + (fence(", ".join(online)) if online else "nobody"))
        return ToolOutput("\n".join(lines), result_brief=f"{status.get('state')}, {len(online)} on")

    async def mc_players(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if str(arguments.get("gamertag") or "").strip():
            return await where_is(str(arguments["gamertag"]).strip(), ctx)
        status = await sidecar("GET", "/status")
        online = (
            {
                mc_sessions.player_key(str(p.get("xuid", "")), str(p.get("name", ""))): float(
                    p.get("joined_at") or 0
                )
                for p in status.get("players", [])
            }
            if isinstance(status, dict)
            else {}
        )
        rows = await mc_sessions.players(maker, online, ctx=ctx.session)
        if not rows:
            return ToolOutput("Nobody has played on this server yet.", result_brief="none yet")
        body = "\n".join(
            f"{r['gamertag']}: {'online now, ' if r['online'] else ''}"
            f"{_duration(r['total_seconds'])} over {r['sessions']} session(s),"
            f" last seen {_when(datetime.fromtimestamp(r['last_seen'], UTC))}"
            for r in rows
        )
        return ToolOutput(fence(body), result_brief=f"{len(rows)} player(s)")

    async def where_is(raw: str, ctx: ToolContext) -> str | ToolOutput:
        """One player's whereabouts: live while they're online, else their last sample in
        the loaded world. The owner decided Dave may say where other players are (§4), so
        this answers in game for anyone, unlike the tools that write."""
        tag = check_gamertag(raw)
        if tag is None:
            return "That isn't a gamertag (letters, digits and spaces, up to 16)."
        world = await loaded_world(ctx)
        status = await sidecar("GET", "/status")
        async with scoped_session(maker, ctx.session) as s:
            seen = (
                await s.execute(
                    text(
                        "SELECT xuid, gamertag FROM app.mc_player_sessions"
                        " WHERE lower(gamertag) = lower(:g) ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"g": tag},
                )
            ).first()
            last = (
                (
                    await s.execute(
                        text(
                            "SELECT x, y, z, dim, at FROM app.mc_player_track"
                            " WHERE xuid = :k AND world = :w ORDER BY at DESC LIMIT 1"
                        ),
                        {"k": seen.xuid, "w": world},
                    )
                ).first()
                if seen is not None and world
                else None
            )
        if seen is None:
            return ToolOutput(
                f"Nobody called {fence(tag)} has played on this server.", result_brief="unknown"
            )
        name = str(seen.gamertag)
        online = isinstance(status, dict) and any(
            str(p.get("name", "")).casefold() == name.casefold()
            or (p.get("xuid") and str(p.get("xuid")) == seen.xuid)
            for p in status.get("players", [])
        )
        if last is None:
            state = "online now" if online else "not online"
            return Found(
                f"{fence(name)} is {state}, and has no position recorded in this world yet.",
                "no position",
                gamertag=name,
                online=online,
                spot=None,
            )
        spot = Spot(math.floor(last.x), math.floor(last.y), math.floor(last.z), str(last.dim))
        asker = asker_for(ctx.agent_session_id)
        if online:
            body = f"{fence(name)} is online now, at {spot_text(spot)}." + from_asker(asker, spot)
        else:
            body = (
                f"{fence(name)} isn't online. Last seen at {spot_text(spot)},"
                f" {_when(last.at)}." + from_asker(asker, spot)
            )
        return Found(
            body,
            "online" if online else "last seen",
            gamertag=name,
            online=online,
            spot=spot,
        )

    async def mc_play_history(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        days = _int(arguments.get("days"), 14, 1, 90)
        if days is None:
            return "days must be a number of days."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            rows = (
                await s.execute(
                    text(
                        "SELECT started_at, coalesce(ended_at, last_seen_at) AS ended"
                        " FROM app.mc_player_sessions"
                        " WHERE xuid = :k AND started_at >= :since ORDER BY started_at"
                    ),
                    {"k": player.key, "since": datetime.now(UTC) - timedelta(days=days)},
                )
            ).all()
        if not rows:
            return ToolOutput(
                f"No play sessions for {fence(player.gamertag)} in the last {days} days.",
                result_brief="none",
            )
        body = "\n".join(
            f"{_when(r.started_at)} for {_duration((r.ended - r.started_at).total_seconds())}"
            for r in rows
        )
        return ToolOutput(
            f"{fence(player.gamertag)}, last {days} days:\n{body}",
            result_brief=f"{len(rows)} session(s)",
        )

    # --- the world -------------------------------------------------------------------

    async def mc_world_info(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        out: list[str] = []
        day: int | None = None
        for label, command in (
            ("Day", "time query day"),
            (
                "Time of day (ticks, 0 = sunrise, 6000 = noon, 18000 = midnight)",
                "time query daytime",
            ),
            ("Weather", "weather query"),
        ):
            got = await console(command)
            if isinstance(got, str):
                return got
            out.append(f"{label}: {_reply(got)}")
            if command == "time query day":
                day = scans.parse_day(got)
        # The console prints whatever arrives in the wait window — a join line with a
        # gamertag, an add-on's event line — so the reply is fenced like any player text.
        body = fence("\n".join(out))
        asker = asker_for(ctx.agent_session_id)
        if asker is not None and day is not None:
            used, _ = await book_for(ctx).today(asker.world, asker.key, day)
            n = max(scans.SCANS_PER_DAY - used, 0)
            body += (
                f"\nDeep scans for the player asking: {scans.scans_left(n)}"
                f" (of {scans.SCANS_PER_DAY})."
            )
            return Found(body, "time + weather", scans_left=n)
        return ToolOutput(body, result_brief="time + weather")

    async def scanned(
        ctx: ToolContext, search: scans.Search, run: scans.Run, *, needs_server: bool
    ) -> str:
        """A deep scan: unlimited outside the game; in game, within the asker's daily
        limit (§M17), counted against the world's own day."""
        asker = asker_for(ctx.agent_session_id)
        if asker is None:
            out, _ = await run()
            return out
        book = book_for(ctx)
        day = await scans.scan_day(
            book, asker, await console("time query day"), needs_server=needs_server
        )
        if isinstance(day, str):
            return ToolOutput(day, result_brief="no day, not searched")
        return await scans.deep_scan(book, asker, day, search, run)

    async def where(
        arguments: dict, ctx: ToolContext, dim: str = "overworld"
    ) -> tuple[int, int, str] | str:
        """The point a search starts from: the x and z given, else where the in-game
        asker stands (when it's that dimension), else this chat's player's last Overworld
        position in the loaded world, else world spawn. A sentence when the arguments are
        wrong."""
        x, z = arguments.get("x"), arguments.get("z")
        origin = "the coordinates given"
        if (x is None) != (z is None):
            return "Give both x and z, or neither (to search from the player's last position)."
        asker = asker_for(ctx.agent_session_id)
        if x is None and asker is not None and asker.at.dim == dim:
            x, z, origin = asker.at.x, asker.at.z, "where the player is standing"
        if x is None or z is None:
            # Only a point in the LOADED world means anything to its generator: the trail
            # also holds every other slot the player has walked.
            worlds = await sidecar("GET", "/worlds")
            loaded = worlds.get("active") if isinstance(worlds, dict) else None
            last = None
            async with scoped_session(maker, ctx.session) as s:
                player = await chat_player(s, ctx)
                if player is not None and loaded:
                    # In the dimension searched; for the Nether, else the Overworld spot
                    # scaled down, the way a portal maps it.
                    for d in (dim, "overworld") if dim == "nether" else (dim,):
                        row = (
                            await s.execute(
                                text(
                                    "SELECT x, z FROM app.mc_player_track WHERE xuid = :k"
                                    " AND world = :w AND dim = :d"
                                    " ORDER BY at DESC LIMIT 1"
                                ),
                                {"k": player.key, "w": loaded, "d": d},
                            )
                        ).first()
                        if row is not None:
                            scale = 8 if d != dim else 1
                            last = (row.x / scale, row.z / scale)
                            break
            if last is not None:
                (x, z), origin = last, "the player's last known position"
            else:
                x, z, origin = 0, 0, "world spawn (0, 0)"
        try:
            xi, zi = int(float(x)), int(float(z))
        except (TypeError, ValueError):
            return "x and z must be numbers."
        if abs(xi) > _COORD_MAX or abs(zi) > _COORD_MAX:
            return "Those coordinates are outside the world."
        return xi, zi, origin

    async def mc_locate(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        kind = str(arguments.get("kind") or "")
        what = str(arguments.get("id") or "").strip().lower()
        if kind not in ("structure", "biome"):
            return "kind must be structure or biome."
        if not _GAME_ID.fullmatch(what):
            return "That isn't a Bedrock id — e.g. mansion, village, ancient_city, cherry_grove."
        if kind == "biome" and not what.startswith("minecraft:"):
            what = f"minecraft:{what}"  # biomes need the namespace; structures don't (M0b)
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at
        xi, zi, origin = at

        async def run() -> tuple[str, bool]:
            # From the console `locate` runs in the Overworld unless told the dimension.
            got = await console(
                f"execute in {dim} positioned {xi} 64 {zi} run locate {kind} {what}"
            )
            if isinstance(got, str):
                return got, False
            reply = _reply(got)
            place = "" if dim == "overworld" else f" in the {_DIMENSION_NAMES[dim]}"
            out = ToolOutput(
                f"Searching{place} from {origin} ({xi}, {zi}): {fence(reply)}", result_brief=what
            )
            return out, bool(_LOCATED.search(reply))

        search = scans.Search("mc_locate", f"{kind}:{what}", dim, xi, zi)
        return await scanned(ctx, search, run, needs_server=True)

    async def mc_find_mob(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """The nearest mob of a type around a player, asked of the game itself (the pack's
        `jb:find`): mobs exist only in the chunks around online players, so this is a
        look around, not a deep scan, and costs none."""
        mob = str(arguments.get("mob") or "").strip().lower().replace(" ", "_")
        if not _GAME_ID.fullmatch(mob):
            return "That isn't a Bedrock mob id — e.g. cow, sheep, horse, villager, zombie."
        reach = _int(arguments.get("max_distance"), 256, 8, 256)
        if reach is None:
            return "max_distance must be a number of blocks."
        who = await waypoint_player(arguments, ctx)
        if isinstance(who, str):
            return ToolOutput(who, result_brief="refused") if who == only_you else who
        _, gamertag = who
        query = {"player": gamertag, "type": mob, "max": reach}
        got = await console(f"scriptevent jb:find {json.dumps(query)}")
        if isinstance(got, str):
            return got
        reply = _reply(got)
        if "find refused" in reply:
            return ToolOutput(
                f"The search couldn't run: {fence(reply)}", result_brief="not searched"
            )
        if "find none" in reply:
            return ToolOutput(
                f"No {mob.removeprefix('minecraft:').replace('_', ' ')} within {reach} blocks"
                f" of {fence(gamertag)}. Mobs only exist in the chunks around online players,"
                " so this looked nearby only.",
                result_brief="none nearby",
            )
        m = _MOB_HIT.search(reply)
        if m is None:
            return (
                "The server took the command, but the jbrain pack didn't answer — it may not"
                " have loaded in this world yet (it updates when the server restarts)."
            )
        spot = Spot(int(m["x"]), int(m["y"]), int(m["z"]), m["dim"])
        return Found(
            f"The nearest {m['type'].removeprefix('minecraft:').replace('_', ' ')} to"
            f" {fence(gamertag)} is at x={spot.x} y={spot.y} z={spot.z}, {m['blocks']} blocks"
            f" away (in the {_DIMENSION_NAMES[spot.dim]}). Mobs wander: this is where it is"
            " now. Point their Tricorder at it with mc_set_waypoint if they want to go.",
            m["type"].removeprefix("minecraft:"),
            target=spot,
        )

    def dimension_of(arguments: dict, ctx: ToolContext) -> str:
        # In game, "here" is the dimension the asker is standing in.
        asker = asker_for(ctx.agent_session_id)
        return str(arguments.get("dimension") or (asker.at.dim if asker else "overworld"))

    async def mc_what_is_at(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at
        xi, zi, origin = at
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, str):
            return worlds
        slot = next((w["id"] for w in worlds.get("slots", []) if w.get("active")), "slot1")
        got = await sidecar(
            "GET", "/map/point", params={"slot": slot, "dim": dim, "x": xi, "z": zi}
        )
        if isinstance(got, str):
            return got
        where_ = f"({xi}, {zi}) in the {_DIMENSION_NAMES[dim]}, {origin}"
        if got.get("source") == "unknown":
            return ToolOutput(
                f"Nothing is known at {where_}: nobody has explored it, and the"
                " satellite's survey shows no ground there (the End's open void, or a world"
                " whose seed isn't known).",
                result_brief="unknown",
            )
        how = (
            "explored ground"
            if got["source"] == "explored"
            else "the satellite's survey (predicted from the seed; the height is within a"
            " few blocks)"
        )
        return Found(
            f"{where_}: {got['biome']} ({got['biome_id']}), surface at y={got['y']} — {how}.",
            str(got["biome"]),
            biome=str(got["biome"]),
        )

    async def mc_find_block(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        raw = arguments.get("blocks")
        names = [str(b).strip().lower() for b in (raw if isinstance(raw, list) else [raw]) if b]
        if not names or len(names) > 8 or not all(_GAME_ID.fullmatch(n) for n in names):
            return 'blocks is a list of Bedrock block ids, e.g. ["diamond_ore"].'
        limit = _int(arguments.get("limit"), 5, 1, 20)
        radius = _int(arguments.get("radius"), 2048, 16, 16384)
        if limit is None or radius is None:
            return "limit and radius must be numbers."
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at
        xi, zi, origin = at
        search = scans.Search(
            "mc_find_block", f"{','.join(sorted(set(names)))}|r{radius}|n{limit}", dim, xi, zi
        )

        async def run() -> tuple[str, bool]:
            out = await find_blocks(names, dim, xi, zi, origin, limit, radius)
            return out, out.result_brief not in ("", "none explored")

        return await scanned(ctx, search, run, needs_server=False)

    async def find_blocks(
        names: list[str], dim: str, xi: int, zi: int, origin: str, limit: int, radius: int
    ) -> ToolOutput:
        """The search itself. A bare sentence (no `result_brief`) is a failure that searched
        nothing, so it costs no scan."""
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, str):
            return ToolOutput(worlds)
        slot = next((w["id"] for w in worlds.get("slots", []) if w.get("active")), "slot1")
        got = await sidecar(
            "GET",
            "/map/find",
            params={
                "slot": slot,
                "dim": dim,
                "x": xi,
                "z": zi,
                "blocks": ",".join(names),
                "limit": limit,
                "radius": radius,
            },
        )
        if isinstance(got, str):
            return ToolOutput(got)
        head = f"From ({xi}, {zi}) in the {_DIMENSION_NAMES[dim]}, {origin}"
        if not got.get("searched"):
            hint = got.get("suggestions") or []
            return ToolOutput(
                f"{head}: no {', '.join(names)} anywhere in explored ground."
                + (f" Block ids that are there: {', '.join(hint)}." if hint else "")
                + " Unexplored ground can't be searched — blocks don't exist until the game"
                " generates the chunk.",
                result_brief="none explored",
            )
        found = got.get("found") or []
        if not found:
            return ToolOutput(
                f"{head}: none within {got.get('radius')} blocks in the"
                f" {got.get('explored_chunks_in_range', 0)} explored chunks there.",
                result_brief="none in range",
            )
        lines = [
            f"- {h['block'].removeprefix('minecraft:').replace('_', ' ')} at x={h['x']}"
            f" y={h['y']} z={h['z']}, {h['distance']} blocks away (across, not counting depth)"
            for h in found
        ]
        return ToolOutput(
            f"{head}, nearest first, explored ground only:\n" + "\n".join(lines),
            result_brief=f"{len(found)} found",
        )

    async def mc_find_item(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """What's inside the world's containers, and lying on the ground near a player
        (§M23). In game a search within NEAR_BLOCKS of the asker is their Tricorder's
        short-range scan and free; anything wider is a deep scan (§M17). The owner's app
        chats are unlimited."""
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        raw = arguments.get("items")
        names = [
            str(i).strip().lower().replace(" ", "_")
            for i in (raw if isinstance(raw, list) else [raw])
            if i
        ]
        if not names or len(names) > 8 or not all(_ITEM_ID.fullmatch(n) for n in names):
            return 'items is a list of 1-8 Bedrock item ids, e.g. ["diamond", "iron_ingot"].'
        limit = _int(arguments.get("limit"), 5, 1, 20)
        wide = arguments.get("radius") in (None, "")
        radius = None if wide else _int(arguments.get("radius"), NEAR_BLOCKS, 1, 60_000_000)
        if limit is None or (radius is None and not wide):
            return "limit and radius must be numbers."
        asker = asker_for(ctx.agent_session_id)
        if asker is not None:
            raw_tag = str(arguments.get("gamertag") or "").strip()
            if raw_tag and raw_tag.casefold() != asker.gamertag.casefold():
                return ToolOutput(only_you, result_brief="refused")
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at
        xi, zi, origin = at
        # Who to look around on the ground: in game the asker (it's their Tricorder), in
        # the app only a player named — the ground is only loaded around online players.
        looker: str | None = None
        if asker is not None and asker.at.dim == dim:
            looker = asker.gamertag
        elif asker is None and arguments.get("gamertag"):
            looker = check_gamertag(str(arguments["gamertag"]))
            if looker is None:
                return "That isn't a gamertag (letters, digits and spaces, up to 16)."
        near = (
            asker is not None
            and asker.at.dim == dim
            and radius is not None
            and radius <= NEAR_BLOCKS
            and math.dist((xi, zi), (asker.at.x, asker.at.z)) <= NEAR_BLOCKS
        )
        if near and asker is not None:
            # The Tricorder scans around the one carrying it.
            xi, zi, origin = asker.at.x, asker.at.z, "where the player is standing"

        async def run() -> tuple[str, bool]:
            out = await find_items(names, dim, xi, zi, origin, limit, radius, looker, near)
            return out, out.result_brief not in ("", "none anywhere")

        if asker is None or near:
            out, _ = await run()
            return out
        search = scans.Search(
            "mc_find_item",
            f"{','.join(sorted(set(names)))}|r{radius or 'all'}|n{limit}",
            dim,
            xi,
            zi,
        )
        return await scanned(ctx, search, run, needs_server=False)

    async def ground_look(gamertag: str, names: list[str], reach: int) -> list[dict] | str:
        """Stacks on the ground around an online player, from the game itself (the pack's
        `jb:items`), or a sentence saying why there are none to show."""
        query = {"player": gamertag, "types": names, "max": reach}
        got = await console(f"scriptevent jb:items {json.dumps(query)}")
        if isinstance(got, str):
            return got
        reply = _reply(got)
        hits = [
            {
                "item": m["type"],
                "count": int(m["n"]),
                "spot": Spot(int(m["x"]), int(m["y"]), int(m["z"]), m["dim"]),
                "distance": int(m["blocks"]),
            }
            for m in _GROUND_HIT.finditer(reply)
        ]
        if hits or "items none" in reply:
            return hits
        if "items refused" in reply:
            return f"Nothing was looked for on the ground: {fence(reply)}"
        return (
            "The ground couldn't be looked at: the jbrain pack didn't answer (it updates"
            " when the server restarts)."
        )

    async def find_items(
        names: list[str],
        dim: str,
        xi: int,
        zi: int,
        origin: str,
        limit: int,
        radius: int | None,
        looker: str | None,
        same_origin: bool = False,
    ) -> ToolOutput:
        """The search itself: the containers from the world's files, then the ground
        around `looker`. A bare sentence (no `result_brief`) searched nothing.
        `same_origin`: the containers were searched around the player too, so the two
        lists' distances compare."""
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, str):
            return ToolOutput(worlds)
        slot = next((w["id"] for w in worlds.get("slots", []) if w.get("active")), "slot1")
        params: dict[str, Any] = {
            "slot": slot,
            "dim": dim,
            "x": xi,
            "z": zi,
            "items": ",".join(names),
            "limit": limit,
            "flush": 1,
        }
        if radius is not None:
            params["radius"] = radius
        got = await sidecar("GET", "/map/items", params=params)
        if isinstance(got, str):
            return ToolOutput(got)
        where_ = f"within {radius} blocks" if radius is not None else "anywhere in the dimension"
        head = (
            f"Inside chests and other containers {where_} of ({xi}, {zi}) in the"
            f" {_DIMENSION_NAMES[dim]}, {origin}{as_of(got.get('age_s'))}"
        )
        found = got.get("found") or []
        # Not one of the names is in any container at all: most likely a wrong id.
        nowhere = len(got.get("in_no_container") or []) >= len(got.get("searched") or names)
        box_lines: list[tuple[int, str]] = []
        named: list[str] = []
        for h in found:
            what = ", ".join(f"{n} {_item_words(i)}" for i, n in h["items"].items())
            boxed = sum((h.get("in_shulker_boxes") or {}).values())
            inside = f" ({boxed} of them in shulker boxes)" if boxed else ""
            box_lines.append(
                (
                    int(h["distance"]),
                    f"- {what} in a {h['kind']} at x={h['x']} y={h['y']} z={h['z']},"
                    f" {h['distance']} blocks away{inside}",
                )
            )
            if h.get("name"):
                named.append(f"({h['x']}, {h['y']}, {h['z']}): {h['name']}")
        if found:
            totals = ", ".join(f"{n} {_item_words(i)}" for i, n in got["totals"].items())
            more = int(got.get("matched", len(found))) - len(found)
            body = f"{head}, nearest first:\n" + "\n".join(t for _, t in box_lines)
            body += f"\nIn all, {totals} in {got.get('matched')} container(s)"
            body += f" ({more} more not listed)." if more > 0 else "."
        elif nowhere:
            hint = got.get("suggestions") or []
            body = f"{head}: no {', '.join(names)} in any container in the dimension." + (
                f" Item ids that are in containers: {', '.join(hint)}." if hint else ""
            )
        else:
            body = f"{head}: none, though some are in containers farther away."
        if got.get("unopened_loot"):
            body += (
                f" {got['unopened_loot']} loot chest(s) in the world haven't been opened yet;"
                " what's in them doesn't exist until someone opens one."
            )
        ground: list[dict] | str = []
        if looker is not None:
            ground = await ground_look(looker, names, NEAR_BLOCKS)
        ground_lines: list[tuple[int, str]] = []
        if isinstance(ground, str):
            body += f"\n{ground}"
        elif looker is not None:
            for g in ground[:limit]:
                spot = g["spot"]
                there = "" if spot.dim == dim else f" (in the {_DIMENSION_NAMES[spot.dim]})"
                ground_lines.append(
                    (
                        g["distance"],
                        f"- {g['count']} {_item_words(g['item'])} on the ground at x={spot.x}"
                        f" y={spot.y} z={spot.z}, {g['distance']} blocks away{there}",
                    )
                )
            ground_head = f"On the ground within {NEAR_BLOCKS} blocks of the player, now"
            ground_text = (
                f"{ground_head}:\n" + "\n".join(t for _, t in ground_lines)
                if ground_lines
                else f"{ground_head}: none (things on the ground vanish after 5 minutes)."
            )
            # Whichever holds the nearest hit comes first: it's the one a Tricorder is aimed at.
            # Ground distances are from the player, the containers' from the search's centre,
            # so only a search centred on the player can rank one against the other (#1695).
            nearer = not box_lines or ground_lines and ground_lines[0][0] < box_lines[0][0]
            if ground_lines and same_origin and nearer:
                body = f"{ground_text}\n{body}"
            else:
                body = f"{body}\n{ground_text}"
        if named:
            body += "\nNames players gave those containers:\n" + fence("\n".join(named))
        if found or ground_lines:
            brief = f"{len(found)} container(s)" + (
                f", {len(ground_lines)} on the ground" if ground_lines else ""
            )
        else:
            brief = "none anywhere" if nowhere else "none in range"
        return ToolOutput(body, result_brief=brief)

    async def active_slot() -> str | ToolOutput:
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, str):
            return ToolOutput(worlds)
        return next((w["id"] for w in worlds.get("slots", []) if w.get("active")), "slot1")

    async def near_or_deep(
        ctx: ToolContext,
        dim: str,
        at: tuple[int, int, str],
        radius: int | None,
        search: Callable[[int, int, str, int | None], Awaitable[ToolOutput]],
        key: str,
    ) -> str | ToolOutput:
        """The chest rule (§M23), which villagers and pets follow too (owner,
        2026-10-13): in game, what's within NEAR_BLOCKS of the asker is their Tricorder's
        short-range scan, free; anything farther is a deep scan (§M17), a repeat from
        about the same place free. `search` answers with `result_brief` "none in range"
        when it found nothing there, "none anywhere" when nothing in the whole world
        matched (a deep scan that found that is given back), and no brief when it
        couldn't search at all. The app is unlimited."""
        xi, zi, origin = at
        asker = asker_for(ctx.agent_session_id)
        if asker is None:
            return await search(xi, zi, origin, radius)
        near = asker.at.dim == dim and math.dist((xi, zi), (asker.at.x, asker.at.z)) <= NEAR_BLOCKS
        if near:
            reach = NEAR_BLOCKS if radius is None else min(radius, NEAR_BLOCKS)
            out = await search(asker.at.x, asker.at.z, "where the player is standing", reach)
            done = out.result_brief not in ("none in range", "none anywhere")
            if done or (radius is not None and radius <= NEAR_BLOCKS):
                if out.result_brief and out.result_brief != "owner unknown":
                    return ToolOutput(
                        f"{out}\n(The Tricorder's short-range scan, {NEAR_BLOCKS} blocks"
                        " around the player: free.)",
                        result_brief=out.result_brief,
                    )
                return out

        async def run() -> tuple[str, bool]:
            out = await search(xi, zi, origin, radius)
            return out, out.result_brief not in ("", "none anywhere")

        scan = scans.Search(key.split("|", 1)[0], key.split("|", 1)[1], dim, xi, zi)
        return await scanned(ctx, scan, run, needs_server=False)

    async def mc_find_villager(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """Villagers by job, or by what they sell, from the world's files (§M4e). Only
        the trades each offers now (owner, 2026-10-13)."""
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        words = {}
        for arg in ("profession", "sells", "enchantment"):
            raw = str(arguments.get(arg) or "").strip().lower().replace(" ", "_")
            raw = raw.removeprefix("minecraft:")
            if raw and not _WORD_ID.fullmatch(raw):
                return f"{arg} is one word id, e.g. librarian, enchanted_book, mending."
            words[arg] = raw
        limit = _int(arguments.get("limit"), 5, 1, 20)
        wide = arguments.get("radius") in (None, "")
        radius = None if wide else _int(arguments.get("radius"), NEAR_BLOCKS, 1, 60_000_000)
        if limit is None or (radius is None and not wide):
            return "limit and radius must be numbers."
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at
        asker = asker_for(ctx.agent_session_id)

        async def search(xi: int, zi: int, origin: str, reach: int | None) -> ToolOutput:
            slot = await active_slot()
            if isinstance(slot, ToolOutput):
                return slot
            params: dict[str, Any] = {
                "slot": slot,
                "dim": dim,
                "x": xi,
                "z": zi,
                "limit": limit,
                "flush": 1,
                "profession": words["profession"],
                "sells": words["sells"],
                "enchant": words["enchantment"],
            }
            if reach is not None:
                params["radius"] = reach
            got = await sidecar("GET", "/map/villagers", params=params)
            if isinstance(got, str):
                return ToolOutput(got)
            where_ = f"within {reach} blocks" if reach is not None else "anywhere in the dimension"
            head = (
                f"Villagers {where_} of ({xi}, {zi}) in the {_DIMENSION_NAMES[dim]}, {origin},"
                f" from the saved world{as_of(got.get('age_s'))}"
            )
            found = got.get("found") or []
            if not found:
                brief = "none anywhere" if reach is None else "none in range"
                return ToolOutput(f"{head}: none that match.", result_brief=brief)
            lines, names = [], []
            for i, v in enumerate(found, 1):
                way, far = bearing(xi, zi, v["x"], v["z"])
                job = str(v["job"]).replace("_", " ")
                who = (
                    _a(job)
                    if v["kind"] == "wandering_trader" or job in ("unemployed", "nitwit")
                    else f"{_a(v['level_name'])} {job}"
                )
                locked = (
                    " (its job is locked in: someone has traded with it)"
                    if v.get("locked_in")
                    else ""
                )
                line = (
                    f"- #{i} {who} at x={v['x']} y={v['y']} z={v['z']}, {far} blocks {way}{locked}"
                )
                trades = v.get("trades") or []
                if trades:
                    line += ": sells " + "; ".join(trade_text(t) for t in trades[:8])
                    if len(trades) > 8:
                        line += f"; and {len(trades) - 8} more"
                lines.append(line)
                if v.get("name"):
                    names.append(f"#{i}: {v['name']}")
            body = f"{head}, nearest first:\n" + "\n".join(lines)
            more = int(got.get("matched", len(found))) - len(found)
            if more > 0:
                body += f"\n({more} more not listed.)"
            body += (
                "\nOnly the trades each offers now: a villager's higher levels unlock more,"
                " and Dave never tells those."
            )
            if names:
                body += "\nNames players gave them:\n" + fence("\n".join(names))
            if asker is not None:
                body += (
                    "\nPoint their Tricorder at the one they want with mc_set_waypoint, then"
                    " answer in Dave's words, e.g.: From up here I see a librarian 230 blocks"
                    " west who sells Mending for 12 emeralds."
                )
            return ToolOutput(body, result_brief=f"{len(found)} villager(s)")

        key = (
            "mc_find_villager|"
            + ",".join(f"{k}={v}" for k, v in sorted(words.items()))
            + f"|r{radius or 'all'}|n{limit}"
        )
        return await near_or_deep(ctx, dim, at, radius, search, key)

    async def mc_find_pet(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """Pets and named mobs from the world's files (§M4e). Anyone's pets can be found,
        in game too (owner, 2026-10-13), and any named mob; the chest rule prices it."""
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        kind = pet_kind(arguments.get("kind"))
        if kind and not _WORD_ID.fullmatch(kind):
            return "kind is a mob id, e.g. wolf (a dog), cat, parrot, horse."
        name = " ".join(
            "".join(c for c in str(arguments.get("name") or "") if c.isprintable()).split()
        )
        if len(name) > 64:
            return "name is at most 64 characters."
        limit = _int(arguments.get("limit"), 5, 1, 20)
        if limit is None:
            return "limit must be a number."
        asker = asker_for(ctx.agent_session_id)
        anyone = bool(arguments.get("anyone"))
        raw_tag = str(arguments.get("gamertag") or "").strip()
        owner: dict[str, str] = {}
        whose = ""
        if raw_tag:
            tag = check_gamertag(raw_tag)
            if tag is None:
                return "That isn't a gamertag (letters, digits and spaces, up to 16)."
            if asker is not None and tag.casefold() == asker.gamertag.casefold() and asker.pid:
                owner = {"owner": asker.pid}
            else:
                owner = {"owner_name": tag}
            whose = tag
        elif asker is not None and not anyone:
            # "My dog": the asker's own, by the id their /dave carried.
            owner = {"owner": asker.pid} if asker.pid else {"owner_name": asker.gamertag}
            whose = asker.gamertag
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at

        async def search(xi: int, zi: int, origin: str, reach: int | None) -> ToolOutput:
            slot = await active_slot()
            if isinstance(slot, ToolOutput):
                return slot
            params: dict[str, Any] = {
                "slot": slot,
                "dim": dim,
                "x": xi,
                "z": zi,
                "limit": limit,
                "flush": 1,
                "kind": kind,
                "name": name,
                **owner,
            }
            if reach is not None:
                params["radius"] = reach
            else:
                params["everywhere"] = 1
            got = await sidecar("GET", "/map/pets", params=params)
            if isinstance(got, str):
                return ToolOutput(got)
            if got.get("owner_known") is False and not name:
                return ToolOutput(
                    f"Dave hasn't learned which pets are {fence(whose)}'s yet: the world's"
                    " files name a pet's owner only by a number, which Dave learns the first"
                    " time that player asks him something in game. Ask them to, or search"
                    " by the pet's name.",
                    result_brief="owner unknown",
                )
            where_ = f"within {reach} blocks" if reach is not None else "anywhere in the world"
            what = "mobs with that name" if name else "pets"
            mine = f"{fence(whose)}'s " if whose and not name else ""
            head = (
                f"{f'{mine}{what}' if mine else what.capitalize()} {where_} of ({xi}, {zi}) in"
                f" the {_DIMENSION_NAMES[dim]}, {origin}, from the saved world"
                f"{as_of(got.get('age_s'))}"
            )
            found = got.get("found") or []
            if not found:
                brief = "none anywhere" if reach is None else "none in range"
                return ToolOutput(f"{head}: none.", result_brief=brief)
            lines, names = [], []
            for i, p in enumerate(found, 1):
                what_ = str(p["kind"]).replace("_", " ")
                if p.get("coat"):
                    what_ = f"{' '.join(p['coat'])} {what_}"
                if p.get("baby"):
                    what_ = f"baby {what_}"
                state = ", sitting" if p.get("sitting") else ""
                there = p.get("dimension") or dim
                if p.get("distance") is None or there != dim:
                    away = f"in the {_DIMENSION_NAMES.get(there, there)}"
                else:
                    way, far = bearing(xi, zi, p["x"], p["z"])
                    away = f"{far} blocks {way}"
                tamed = " (tamed)" if p.get("tamed") and name else ""
                line = (
                    f"- #{i} {_a(what_)}{tamed}{state} at x={p['x']} y={p['y']} z={p['z']}, {away}"
                )
                lines.append(line)
                label = []
                if p.get("name"):
                    label.append(f"name {p['name']}")
                if p.get("owner_name"):
                    label.append(f"owner {p['owner_name']}")
                if label:
                    names.append(f"#{i}: " + ", ".join(label))
            body = f"{head}, nearest first:\n" + "\n".join(lines)
            more = int(got.get("matched", len(found))) - len(found)
            if more > 0:
                body += f"\n({more} more not listed.)"
            if names:
                body += "\nTheir names and owners, as players wrote them:\n" + fence(
                    "\n".join(names)
                )
            body += (
                "\nAn animal wanders unless it's sitting: this is where the world last saved it."
            )
            if asker is not None:
                body += (
                    "\nPoint their Tricorder at it with mc_set_waypoint (its dimension too),"
                    " then answer in Dave's words, e.g.: Rex is 120 blocks north-east,"
                    " sitting right where you left him. Hold your Tricorder and follow the"
                    ' arrow. Say "sitting right where you left him" only for one that is'
                    " sitting."
                )
            return ToolOutput(body, result_brief=f"{len(found)} found")

        key = (
            f"mc_find_pet|kind={kind},name={name.casefold()},owner="
            f"{owner.get('owner') or owner.get('owner_name', '').casefold()}|n{limit}"
        )
        return await near_or_deep(ctx, dim, at, None, search, key)

    async def portals_via(target: Spot, asker: Asker | None) -> list[dict[str, Any]]:
        """The known, still-standing portals on the far side of a target across the
        Overworld and the Nether (§M4e), nearest first to where the player will stand
        there: the asker if they're on that side, else the target's own spot mapped
        across (÷8 into the Nether, ×8 out of it). Empty when none, or on any failure:
        the waypoint still works without them."""
        side = _PORTAL_SIDES.get(target.dim)
        if side is None:
            return []
        slot = await active_slot()
        if isinstance(slot, ToolOutput):
            return []
        got = await sidecar("GET", "/map/portals", params={"slot": slot, "dim": side})
        if isinstance(got, str):
            return []
        if asker is not None and asker.at.dim == side:
            fx, fz = asker.at.x, asker.at.z
        else:
            scale = 1 / 8 if side == "nether" else 8
            fx, fz = target.x * scale, target.z * scale
        found = [
            {"dim": side, "x": int(p["x"]), "z": int(p["z"])}
            for p in got.get("portals") or []
            if isinstance(p, dict) and isinstance(p.get("x"), int) and isinstance(p.get("z"), int)
        ]
        found.sort(key=lambda p: math.dist((fx, fz), (p["x"], p["z"])))
        return found[:VIA_MAX]

    async def loaded_world(ctx: ToolContext) -> str:
        """The world a place is filed under: the one the in-game asker is in, else the
        loaded world's folder; "" when the server can't say."""
        asker = asker_for(ctx.agent_session_id)
        if asker is not None:
            return asker.world
        worlds = await sidecar("GET", "/worlds")
        return str(worlds.get("active") or "") if isinstance(worlds, dict) else ""

    async def waypoint_player(arguments: dict, ctx: ToolContext) -> tuple[Player | None, str] | str:
        """Whose tricorder: a gamertag given (refused in game unless it's the asker),
        else this chat's player. The Player is None when only a gamertag is known."""
        raw_tag = str(arguments.get("gamertag") or "").strip()
        asker = asker_for(ctx.agent_session_id)
        if asker is not None:
            if raw_tag and raw_tag.casefold() != asker.gamertag.casefold():
                return only_you
            return Player(asker.key, asker.gamertag), asker.gamertag
        if raw_tag:
            gamertag = check_gamertag(raw_tag)
            if gamertag is None:
                return "That isn't a gamertag (letters, digits and spaces, up to 16)."
            if not arguments.get("place"):
                return None, gamertag
            async with scoped_session(maker, ctx.session) as s:
                found = await resolve_player(s, gamertag)
            return found, found.gamertag
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
        if player is None:
            return no_player
        return player, player.gamertag

    async def place_target(
        name: str, player: Player | None, ctx: ToolContext
    ) -> tuple[Spot, str] | str:
        """A saved place's spot and label, or why there isn't one. "home" with no saved
        home is the in-game asker's bed, when the server reported one."""
        world = await loaded_world(ctx)
        rows: list[Any] = []
        if player is not None and world:
            async with scoped_session(maker, ctx.session) as s:
                rows = await _places(s, player.key, world)
        row = next((r for r in rows if r.name.casefold() == name.casefold()), None)
        if row is not None:
            return _spot(row), str(row.name)
        asker = asker_for(ctx.agent_session_id)
        home = name.casefold() == "home"
        if home and asker is not None and asker.spawn is not None:
            return asker.spawn, "Home (your bed)"
        known = ", ".join(r.name for r in rows)
        why = " and no bed or respawn anchor set" if home and asker is not None else ""
        return (
            f"There's no saved place called {fence(name)}{why}, so the tricorder wasn't"
            " changed. Offer to mark it: the player stands there and asks you to"
            " (mc_place_save)." + (f" Their saved places: {fence(known)}." if known else "")
        )

    async def mc_set_waypoint(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        clear = bool(arguments.get("clear"))
        who = await waypoint_player(arguments, ctx)
        if isinstance(who, str):
            return ToolOutput(who, result_brief="refused") if who == only_you else who
        player, gamertag = who
        event: dict[str, Any] = {"player": gamertag}
        target: Spot | None = None
        label = "".join(c for c in str(arguments.get("label") or "") if c.isprintable())
        if clear:
            # Asked for, so a new heading (spawn) that ends a live scan (§M25); the track
            # drain's clear on arrival sends no end_scan, so reaching a place doesn't.
            event.update({"clear": True, "end_scan": True})
        elif arguments.get("place"):
            name = place_name(arguments["place"])
            if not name:
                return f"place is a saved place's name, 1–{PLACE_NAME_MAX} characters."
            found = await place_target(name, player, ctx)
            if isinstance(found, str):
                return ToolOutput(found, result_brief="no such place")
            target, saved_label = found
            label = label or saved_label
        else:
            dim = dimension_of(arguments, ctx)
            if dim not in _DIMENSIONS:
                return "dimension must be overworld, nether or the_end."
            try:
                x, z = int(float(arguments["x"])), int(float(arguments["z"]))
                y = None if arguments.get("y") in (None, "") else int(float(arguments["y"]))
            except (KeyError, TypeError, ValueError):
                return (
                    "Give the waypoint's x and z (and y if it matters, like a deep ore), or a"
                    " saved place's name."
                )
            if max(abs(x), abs(z)) > _COORD_MAX or (y is not None and not -64 <= y <= 320):
                return "Those coordinates are outside the world."
            target = Spot(x, y, z, dim)
        via: list[dict[str, Any]] = []
        if target is not None:
            # The label shows on the player's screen: one short line of plain text.
            event.update(
                {
                    "x": target.x,
                    "y": target.y,
                    "z": target.z,
                    "dim": target.dim,
                    "label": label.strip()[:40] or "Waypoint",
                }
            )
            via = await portals_via(target, asker_for(ctx.agent_session_id))
            if via:
                event["via"] = via
        # json.dumps escapes every quote and control character, so the message can't end
        # the command or start another; the console takes it as one line.
        got = await console(f"scriptevent jb:waypoint {json.dumps(event)}")
        if isinstance(got, str):
            return got
        reply = _reply(got)
        if "waypoint refused" in reply:
            return f"The tricorder add-on refused it: {fence(reply)}"
        if "waypoint set" not in reply and "waypoint cleared" not in reply:
            return (
                "The server took the command, but the tricorder add-on didn't answer — the"
                " box's own jbrain pack may not have loaded in this world (check the server"
                " log for its `[jbrain]` lines)."
            )
        # The map's copy of the target (the pack's own can't be read back): what it draws,
        # and what the track drain clears when the player gets there.
        world = await loaded_world(ctx)
        if world:
            book = targets_for(ctx)
            if target is None:
                await book.clear(world, gamertag)
            else:
                await book.set(world, gamertag, target, str(event["label"]))
        ended = " That ended their Tricorder's scan." if "scan ended" in reply else ""
        if clear or target is None:
            return ToolOutput(
                f"Cleared {fence(gamertag)}'s tricorder waypoint; it points at spawn again."
                + ended,
                result_brief="cleared",
            )
        portal_note = (
            f" From the {_DIMENSION_NAMES[via[0]['dim']]} it points at the nearest of"
            f" {len(via)} known Nether portal(s) there."
            if via
            else ""
        )
        return Found(
            f"{fence(gamertag)}'s tricorder now points to {fence(event['label'])} at"
            f" {spot_text(target)}. They see it when they hold the Tricorder."
            + portal_note
            + ended
            + from_asker(asker_for(ctx.agent_session_id), target),
            str(event["label"]),
            target=target,
        )

    async def mc_tricorder_scan(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """The Tricorder's live scan (§M25): the pack finds the nearest block of a kind
        within 16 blocks every 10 s and the held arrow points at it, until a stop or a
        new waypoint. Its own short-range sense: free, never a deep scan. Whose Tricorder
        follows mc_set_waypoint: the asker in game, any gamertag in the app."""
        if arguments.get("status"):
            return await scan_status(arguments, ctx)
        stop = bool(arguments.get("stop"))
        block = str(arguments.get("block") or "").strip().lower().replace(" ", "_")
        if not stop and not _ITEM_ID.fullmatch(block):
            return (
                "block is a Bedrock block id, e.g. diamond_ore, ancient_debris, oak_log; or"
                " set stop to end the scan."
            )
        who = await waypoint_player(arguments, ctx)
        if isinstance(who, str):
            return ToolOutput(who, result_brief="refused") if who == only_you else who
        _, gamertag = who
        event: dict[str, Any] = {"player": gamertag}
        event.update({"stop": True} if stop else {"block": block})
        got = await console(f"scriptevent jb:scan {json.dumps(event)}")
        if isinstance(got, str):
            return got
        reply = _reply(got)
        if "scan refused" in reply:
            if "isn't a block" in reply:
                return ToolOutput(
                    f"There's no block called {fence(block)} in this game, so the Tricorder"
                    " wasn't changed. Use its Bedrock block id, e.g. diamond_ore, iron_ore,"
                    " ancient_debris, oak_log.",
                    result_brief="no such block",
                )
            return ToolOutput(f"The Tricorder refused it: {fence(reply)}", result_brief="refused")
        m = _SCAN_SET.search(reply)
        if stop and "scan stopped" in reply:
            if "none was on" in reply:
                return ToolOutput(
                    f"{fence(gamertag)}'s Tricorder wasn't scanning.", result_brief="not scanning"
                )
            return ToolOutput(
                f"Stopped {fence(gamertag)}'s Tricorder scan; it points at their waypoint"
                " again (world spawn if they had none).",
                result_brief="stopped",
            )
        if m is None:
            return (
                "The server took the command, but the jbrain pack didn't answer — it may not"
                " have loaded in this world yet (it updates when the server restarts)."
            )
        return ToolOutput(
            f"{fence(gamertag)}'s Tricorder is scanning for {m['label']} ({m['types']}):"
            " every 10 seconds its arrow points at the nearest one within 16 blocks, and it"
            ' reads "none nearby" when there isn\'t one. It keeps scanning, across sessions,'
            " until they ask you to stop or set a new waypoint. Free: no deep scan.",
            result_brief=m["label"],
        )

    async def scan_status(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """Is a player's Tricorder scanning, and what did it find: the drain's row (the
        pack's own report), read under `ctx.session`. In game only the asker's own."""
        who = await waypoint_player(arguments, ctx)
        if isinstance(who, str):
            return ToolOutput(who, result_brief="refused") if who == only_you else who
        _, gamertag = who
        world = await loaded_world(ctx)
        if not world:
            return "The Minecraft server can't say which world is loaded, so there's no scan."
        async with scoped_session(maker, ctx.session) as s:
            got = (await livescan.read(s, world)).get(livescan.key(gamertag))
        if got is None:
            return ToolOutput(
                f"{fence(gamertag)}'s Tricorder isn't scanning.", result_brief="not scanning"
            )
        name = str(got["gamertag"] or gamertag)
        asker = asker_for(ctx.agent_session_id)
        here: dict[str, Any] | None = None
        online: bool | None
        if asker is not None:
            online = True
            here = {
                "dim": asker.at.dim,
                "x": asker.at.x + 0.5,
                "y": asker.at.y or 0,
                "z": asker.at.z + 0.5,
            }
        else:
            status = await sidecar("GET", "/status")
            online = (
                any(
                    str(p.get("name", "")).casefold() == name.casefold()
                    for p in status.get("players", [])
                )
                if isinstance(status, dict)
                else None
            )
            for p in await travel.latest_positions(maker, world, ctx.session):
                if str(p["gamertag"]).casefold() == name.casefold():
                    here = p
        body = scan_words(name, got, here, online, datetime.now(UTC).timestamp())
        if asker is None:
            body += (
                " To show it, mc_show_map with focus scan and this gamertag as the label"
                " draws the square it searches and the block it found."
            )
        brief = "offline" if online is False else ("found one" if got["found"] else got["state"])
        return ToolOutput(body, result_brief=f"{got['label']} · {brief}")

    # --- saved places ------------------------------------------------------------------

    async def mc_place_save(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = place_name(arguments.get("name"))
        if not name:
            return f"A place needs a name of 1–{PLACE_NAME_MAX} characters, like tree farm."
        asker = asker_for(ctx.agent_session_id)
        world = await loaded_world(ctx)
        if not world:
            return "The Minecraft server can't say which world is loaded, so nothing was saved."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            # A place is only ever where the player is right now, never typed
            # coordinates: a saved place is what a transport may go to, and that must be
            # somewhere they've stood (owner, 2026-10-11).
            if asker is not None:
                spot = asker.at
                origin = "where the player is standing"
            else:
                last = (
                    await s.execute(
                        text(
                            "SELECT x, y, z, dim FROM app.mc_player_track WHERE xuid = :k"
                            " AND world = :w AND at > now() - make_interval(secs => :fresh)"
                            " ORDER BY at DESC LIMIT 1"
                        ),
                        {"k": player.key, "w": world, "fresh": PLACE_FRESH_S},
                    )
                ).first()
                if last is None:
                    return (
                        f"{fence(player.gamertag)} isn't in this world right now, so nothing"
                        " was saved. A place is saved where they're standing: they can ask in"
                        " game, or ask again here while they're there."
                    )
                spot = Spot(
                    math.floor(last.x), math.floor(last.y), math.floor(last.z), str(last.dim)
                )
                origin = "where the player is right now"
            if (
                max(abs(spot.x), abs(spot.z)) > _COORD_MAX
                or (spot.y is not None and not -64 <= spot.y <= 320)
                or spot.dim not in _DIMENSIONS
            ):
                return "Those coordinates are outside the world."
            rows = await _places(s, player.key, world)
            old = next((r for r in rows if r.name.casefold() == name.casefold()), None)
            if old is None and len(rows) >= PLACES_MAX:
                return (
                    f"{fence(player.gamertag)} already has {PLACES_MAX} saved places in this"
                    " world. Remove one first (mc_place_remove)."
                )
            values = {
                "k": player.key,
                "g": player.gamertag,
                "w": world,
                "n": name,
                "x": spot.x,
                "y": spot.y,
                "z": spot.z,
                "d": spot.dim,
            }
            if old is None:
                await s.execute(
                    text(
                        "INSERT INTO app.mc_places (xuid, gamertag, world, name, x, y, z, dim)"
                        " VALUES (:k, :g, :w, :n, :x, :y, :z, :d)"
                    ),
                    values,
                )
            else:
                await s.execute(
                    text(
                        # `updated_at` is when the place was put where it is (the map's
                        # "moved"), so saving it again on the same block keeps it.
                        "UPDATE app.mc_places SET name = :n, gamertag = :g, x = :x, y = :y,"
                        " z = :z, dim = :d, updated_at = CASE WHEN (x, y, z, dim) IS DISTINCT"
                        " FROM (:x, :y, :z, :d) THEN now() ELSE updated_at END WHERE id = :id"
                    ),
                    {**values, "id": old.id},
                )
        was = _spot(old) if old is not None else None
        moved = f" (moved from {spot_text(was)})" if was is not None and was != spot else ""
        return ToolOutput(
            f"Saved {fence(name)} for {fence(player.gamertag)} at {spot_text(spot)}, from"
            f" {origin}{moved}. mc_set_waypoint with place={name!r} points their tricorder"
            " there.",
            result_brief=f"{name} (moved)" if moved else name,
        )

    async def mc_places(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        world = await loaded_world(ctx)
        if not world:
            return "The Minecraft server can't say which world is loaded."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            rows = await _places(s, player.key, world)
        if not rows:
            return ToolOutput(
                f"{fence(player.gamertag)} has no saved places in this world yet.",
                result_brief="none",
            )
        asker = asker_for(ctx.agent_session_id)
        body = "\n".join(
            f"- {r.name}: {spot_text(_spot(r))}.{from_asker(asker, _spot(r))}" for r in rows
        )
        return Found(
            f"{fence(player.gamertag)}'s saved places in this world:\n{fence(body)}",
            f"{len(rows)} place(s)",
            places=[(str(r.name), _spot(r)) for r in rows],
        )

    async def mc_place_remove(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = place_name(arguments.get("name"))
        if not name:
            return "Name the saved place to remove."
        world = await loaded_world(ctx)
        if not world:
            return "The Minecraft server can't say which world is loaded, so nothing changed."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            gone = (
                await s.execute(
                    text(
                        "DELETE FROM app.mc_places WHERE xuid = :k AND world = :w"
                        " AND lower(name) = lower(:n) RETURNING name, x, y, z, dim"
                    ),
                    {"k": player.key, "w": world, "n": name},
                )
            ).first()
        if gone is None:
            return ToolOutput(
                f"{fence(player.gamertag)} has no saved place called {fence(name)}; nothing"
                " changed.",
                result_brief="not found",
            )
        # Their wormhole gates that led there go dormant (§M12). The removal stands
        # whatever the pack answers, so its answer isn't waited on for anything.
        forget = {"op": "forget", "player": player.gamertag, "place": gone.name}
        await console(f"scriptevent jb:wormhole {json.dumps(forget)}")
        return ToolOutput(
            f"Removed {fence(gone.name)} ({spot_text(_spot(gone))}) from"
            f" {fence(player.gamertag)}'s places.",
            result_brief=f"removed {gone.name}",
        )

    async def mc_nearby(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        dim = dimension_of(arguments, ctx)
        if dim not in _DIMENSIONS:
            return "dimension must be overworld, nether or the_end."
        radius = _int(arguments.get("radius"), 2000, 100, 20000)
        if radius is None:
            return "radius must be a number of blocks."
        at = await where(arguments, ctx, dim)
        if isinstance(at, str):
            return at
        xi, zi, origin = at

        async def run() -> tuple[str, bool]:
            got = await sidecar("GET", "/map/nearby", params={"dim": dim, "x": xi, "z": zi})
            if isinstance(got, str):
                return got, False
            near = [n for n in got.get("nearby", []) if n["distance"] <= radius]
            head = (
                f"Within {radius} blocks of ({xi}, {zi}) in the {_DIMENSION_NAMES[dim]}, {origin}"
            )
            if not near:
                return ToolOutput(f"{head}: no structures.", result_brief="none"), True
            lines = [
                f"- {n['structure'].replace('_', ' ')} at ({n['x']}, {n['z']}),"
                f" {n['distance']} blocks away"
                for n in near
            ]
            return ToolOutput(
                head + " (nearest of each kind; temple = desert or jungle temple, witch hut"
                " or igloo; ruins = ocean ruins):\n" + "\n".join(lines),
                result_brief=f"{len(near)} nearby",
            ), True

        search = scans.Search("mc_nearby", f"r{radius}", dim, xi, zi)
        return await scanned(ctx, search, run, needs_server=True)

    # --- goals and the progress log ---------------------------------------------------

    async def mc_goals(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        want = str(arguments.get("status") or "open")
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
        shown = [(i, g) for i, g in enumerate(goals, start=1) if want == "all" or g.status == want]
        if not shown:
            return ToolOutput(
                f"No {'' if want == 'all' else want + ' '}goals for {fence(player.gamertag)}.",
                result_brief="none",
            )
        body = "\n".join(
            f"{i}. [{g.status}] {goal_line(g)}" + (f" — {g.notes}" if g.notes else "")
            for i, g in shown
        )
        return ToolOutput(
            f"Goals for {fence(player.gamertag)}:\n{fence(body)}",
            result_brief=f"{len(shown)} goal(s)",
        )

    async def mc_goal_create(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        title = " ".join(str(arguments.get("title") or "").split())
        notes = " ".join(str(arguments.get("notes") or "").split())
        if not title or len(title) > GOAL_TITLE_MAX:
            return f"A goal needs a title of 1–{GOAL_TITLE_MAX} characters."
        if len(notes) > LOG_MAX:
            return f"Notes are capped at {LOG_MAX} characters."
        counted = check_goal_item(arguments.get("item"), arguments.get("count"))
        if isinstance(counted, str):
            return counted
        item, count = counted
        if (item is None) != (count is None):
            return (
                "item and count go together: the item id and how many they need, e.g."
                ' item "iron_block", count 23. Or leave both out.'
            )
        # The world it's for, as places are filed: the app counts its chests there, and
        # can tell a goal made in another world. Unknown (the server can't say) is NULL,
        # which reads as whichever world is loaded.
        world = await loaded_world(ctx) or None
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            await s.execute(
                text(
                    "INSERT INTO app.mc_goals"
                    " (xuid, gamertag, world, title, notes, item, item_count)"
                    " VALUES (:k, :g, :w, :t, :n, :it, :ic)"
                ),
                {
                    "k": player.key,
                    "g": player.gamertag,
                    "w": world,
                    "t": title,
                    "n": notes,
                    "it": item,
                    "ic": count,
                },
            )
            number = len(await _goals(s, player.key))
        counts = f" ({item} ×{count}; mc_goal_progress counts how many they have)" if item else ""
        return ToolOutput(
            f"Added goal {number} for {fence(player.gamertag)}: {fence(title)}{counts}",
            result_brief=f"goal {number}",
        )

    async def mc_goal_update(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        try:
            number = int(arguments.get("goal") or 0)
        except (TypeError, ValueError):
            return "goal must be a goal number from mc_goals."
        status = arguments.get("status")
        title = " ".join(str(arguments.get("title") or "").split())
        if status is not None and status not in _GOAL_STATUSES:
            return "status must be open, done or abandoned."
        if title and len(title) > GOAL_TITLE_MAX:
            return f"A title is at most {GOAL_TITLE_MAX} characters."
        counted = check_goal_item(arguments.get("item"), arguments.get("count"))
        if isinstance(counted, str):
            return counted
        item, count = counted
        if status is None and not title and item is None and count is None:
            return "Nothing to change — pass a status, a new title, or an item and count."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
            if not 1 <= number <= len(goals):
                return f"There is no goal {number} — {fence(player.gamertag)} has {len(goals)}."
            goal = goals[number - 1]
            if (item is not None or count is not None) and (
                (item or goal.item) is None or (count or goal.item_count) is None
            ):
                return (
                    f"Goal {number} doesn't count anything yet, so it needs both an item and"
                    ' a count, e.g. item "iron_block", count 23.'
                )
            await s.execute(
                text(
                    "UPDATE app.mc_goals SET title = coalesce(:t, title),"
                    " status = coalesce(:st, status),"
                    " item = coalesce(:it, item), item_count = coalesce(:ic, item_count),"
                    " finished_at = CASE WHEN coalesce(:st, status) = 'open' THEN NULL"
                    "   WHEN :st IS NOT NULL AND :st <> status THEN now() ELSE finished_at END"
                    " WHERE id = :id"
                ),
                {"t": title or None, "st": status, "it": item, "ic": count, "id": goal.id},
            )
        counts = (
            f"now counts {item or goal.item} ×{count or goal.item_count}"
            if item is not None or count is not None
            else ""
        )
        change = ", ".join(
            x
            for x in (
                f"now {status}" if status else "",
                f"renamed “{title}”" if title else "",
                counts,
            )
            if x
        )
        return ToolOutput(
            f"Goal {number} ({fence(goal.title)}): {change}.", result_brief=f"goal {number}"
        )

    # --- goal progress: what they carry and what's in their own chests (§M24) ----------

    async def carry_count(gamertag: str, names: list[str]) -> Pocket | str:
        return await carry_pocket(sidecar, gamertag, names)

    async def chest_counts(
        centres: list[tuple[str, Spot]], item: str
    ) -> tuple[list[tuple[str, int]], Any] | str:
        """How many of `item` the containers within NEAR_BLOCKS of each centre hold, each
        container once, toward its nearest centre; and the files' age. A sentence when
        nothing could be counted."""
        worlds = await sidecar("GET", "/worlds")
        if isinstance(worlds, str):
            return worlds
        slot = next((w["id"] for w in worlds.get("slots", []) if w.get("active")), "slot1")
        got = await stock_near(sidecar, slot, [sp for _, sp in centres], [item], flush=True)
        if isinstance(got, str):
            return got
        return [(label, got.per[i].get(item, 0)) for i, (label, _) in enumerate(centres)], got.age_s

    def pick_goal(goals: list[Any], raw: Any, gamertag: str) -> tuple[int, Any] | str:
        if raw not in (None, "", 0):
            try:
                number = int(raw)
            except (TypeError, ValueError):
                return "goal must be a goal number from mc_goals."
            if not 1 <= number <= len(goals):
                return f"There is no goal {number} — {fence(gamertag)} has {len(goals)}."
            return number, goals[number - 1]
        counting = [(i, g) for i, g in enumerate(goals, start=1) if g.status == "open" and g.item]
        if len(counting) == 1:
            return counting[0]
        if not counting:
            return (
                f"{fence(gamertag)} has no open goal that counts an item. Make one with"
                " mc_goal_create (item and count), or give one with mc_goal_update."
            )
        listed = "\n".join(f"{i}. {goal_line(g)}" for i, g in counting)
        return "Which goal? Pass its number:\n" + fence(listed)

    async def mc_goal_progress(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        """How far along a counted goal is: what the player carries plus what's in the
        containers within NEAR_BLOCKS of each of their saved places and of where they
        stand. Their own stock, so free — except, in game, chests near a place beyond the
        Tricorder's reach of them, which is one deep scan per check (owner, 2026-10-12)."""
        asker = asker_for(ctx.agent_session_id)
        raw_tag = str(arguments.get("gamertag") or "").strip()
        if asker is not None and raw_tag and raw_tag.casefold() != asker.gamertag.casefold():
            return ToolOutput(only_you, result_brief="refused")
        world = await loaded_world(ctx)
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
            places = await _places(s, player.key, world) if world else []
        picked = pick_goal(goals, arguments.get("goal"), player.gamertag)
        if isinstance(picked, str):
            return ToolOutput(picked, result_brief="which goal")
        number, goal = picked
        if goal.item is None:
            return ToolOutput(
                f"Goal {number} ({fence(goal.title)}) doesn't count an item. To count it, give"
                " it one with mc_goal_update (item and count).",
                result_brief="nothing to count",
            )
        if not world:
            return "The Minecraft server can't say which world is loaded, so nothing was counted."
        item, need = full_id(goal.item), int(goal.item_count)
        # Whose pockets: in game the asker's; in the app only the goal's own player, by
        # name, while online — never anyone else's toward this goal.
        who: str | None = asker.gamertag if asker is not None else None
        if asker is None and raw_tag:
            who = check_gamertag(raw_tag)
            if who is None:
                return "That isn't a gamertag (letters, digits and spaces, up to 16)."
            if who.casefold() != player.gamertag.casefold():
                return ToolOutput(
                    f"Goal progress counts only {fence(player.gamertag)}'s own pockets;"
                    " nothing was counted.",
                    result_brief="refused",
                )
        notes: list[str] = []
        pocket: Pocket | None = None
        if who is not None:
            got = await carry_count(who, [item])
            if isinstance(got, str):
                notes.append(got)
            else:
                pocket = got
        else:
            notes.append(
                "What they carry wasn't counted: name their gamertag (while they're online)"
                " to count it."
            )
        stand = asker.at if asker is not None else (pocket.spot if pocket else None)
        # Places first, so a chest as near a place as the player goes to the place.
        centres = [(str(r.name), _spot(r)) for r in places]
        far: list[tuple[str, Spot]] = []
        if asker is not None:
            far = [
                (n, sp)
                for n, sp in centres
                if sp.dim != asker.at.dim
                or math.dist((sp.x, sp.z), (asker.at.x, asker.at.z)) > NEAR_BLOCKS
            ]
        if stand is not None:
            centres.append(("you", stand))
        paid: tuple[str, int] | None = None
        search: scans.Search | None = None
        book = book_for(ctx)
        day = scans.UNKNOWN_DAY
        if asker is not None and far:
            # A files-only count never needs the running server, so it always has a day.
            got_day = await scans.scan_day(
                book, asker, await console("time query day"), needs_server=False
            )
            day = got_day if isinstance(got_day, int) else scans.UNKNOWN_DAY
            search = scans.Search(
                "mc_goal_progress",
                f"{item}|" + ",".join(sorted(n.casefold() for n, _ in far)),
                asker.at.dim,
                asker.at.x,
                asker.at.z,
            )
            paid = await scans.charge(book, asker, day, search)
            if paid[0] == "out":
                centres = [c for c in centres if c not in far]
                names = ", ".join(n for n, _ in far)
                notes.append(
                    f"Chests near {fence(names)} weren't counted: they're beyond the"
                    f" Tricorder's reach, which takes a deep scan, and all {scans.SCANS_PER_DAY}"
                    " of today's are used. Say so in Dave's words: " + scans.RECHARGE
                )
        counted: list[tuple[str, int]] = []
        age: Any = None
        if centres:
            # A count that doesn't finish (an error, a cancelled turn) gives the scan back,
            # as `scans.deep_scan` does: a player never pays for nothing.
            spent = paid is not None and paid[0] == "spent" and asker is not None
            try:
                got_chests = await chest_counts(centres, item)
            except BaseException:
                if spent and asker is not None:
                    await book.refund(asker.world, asker.key, day)
                raise
            if isinstance(got_chests, str):
                if spent and asker is not None:
                    await book.refund(asker.world, asker.key, day)
                return got_chests
            counted, age = got_chests
        carried = pocket.counts.get(item, 0) if pocket is not None else None
        if carried is None and not counted:
            return ToolOutput(
                f"Nothing was counted for goal {number} ({fence(goal_line(goal))}). "
                + " ".join(notes)
                + (
                    ""
                    if places
                    else f" {fence(player.gamertag)} has no saved places in this world"
                    " either: suggest saving their base (mc_place_save) so its chests count."
                ),
                result_brief="nothing counted",
            )
        have, head, where_ = tally(need, carried, counted)
        lines = [f"Goal {number}, {fence(goal_line(goal))}: {head}."]
        if where_:
            lines.append(f"Where they are: {fence(where_)}.")
        if counted:
            near = ", ".join(n for n, _ in counted)
            lines.append(
                f"Counted the chests within {NEAR_BLOCKS} blocks of: {fence(near)}{as_of(age)}."
            )
        if not places:
            lines.append(
                f"{fence(player.gamertag)} has no saved places in this world, so no base"
                " chests were counted. Suggest saving their base: they stand there and ask"
                " to save it (mc_place_save), and its chests count from then on."
            )
        if pocket is not None and pocket.boxes:
            lines.append(
                f"They carry {pocket.boxes} shulker box(es); what's inside those can't be"
                " seen, so it isn't counted."
            )
        lines.extend(notes)
        if have >= need and goal.status == "open":
            lines.append(
                f"Ask whether to mark goal {number} done; mark it (mc_goal_update,"
                " status done) only if they say yes."
            )
        if paid is not None and asker is not None and search is not None:
            if paid[0] == "spent":
                await book.remember(
                    asker.world,
                    asker.key,
                    day,
                    {
                        "tool": search.tool,
                        "what": search.what,
                        "dim": search.dim,
                        "x": search.x,
                        "z": search.z,
                        "text": "\n".join(lines),
                    },
                )
                lines.append(
                    f"That used a deep scan for the far places: {scans.scans_left(paid[1])}"
                    " (say so)."
                )
            elif paid[0] == "repeat":
                lines.append(
                    "The far places were counted earlier today from about here, so this"
                    f" recount is free: {scans.scans_left(paid[1])}."
                )
        return ToolOutput("\n".join(lines), result_brief=f"{have} of {need}")

    async def mc_log(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        entry = " ".join(str(arguments.get("text") or "").split())
        if not entry or len(entry) > LOG_MAX:
            return f"A log entry is 1–{LOG_MAX} characters."
        source = "dave" if arguments.get("by_dave") else "owner"
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goal_id = None
            if arguments.get("goal") not in (None, "", 0):
                goals = await _goals(s, player.key)
                try:
                    number = int(arguments["goal"])
                except (TypeError, ValueError):
                    return "goal must be a goal number from mc_goals."
                if not 1 <= number <= len(goals):
                    return f"There is no goal {number} — {fence(player.gamertag)} has {len(goals)}."
                goal_id = goals[number - 1].id
            await s.execute(
                text(
                    "INSERT INTO app.mc_goal_log (xuid, gamertag, goal_id, text, source)"
                    " VALUES (:k, :g, :gid, :t, :src)"
                ),
                {"k": player.key, "g": player.gamertag, "gid": goal_id, "t": entry, "src": source},
            )
        return ToolOutput(
            f"Logged for {fence(player.gamertag)}: {fence(entry)}", result_brief="logged"
        )

    async def mc_log_read(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        limit = _int(arguments.get("limit"), 20, 1, 100)
        if limit is None:
            return "limit must be a number of entries."
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            goals = await _goals(s, player.key)
            number_of = {g.id: i for i, g in enumerate(goals, start=1)}
            params: dict[str, Any] = {"k": player.key, "n": limit}
            only = ""
            if arguments.get("goal") not in (None, "", 0):
                try:
                    number = int(arguments["goal"])
                except (TypeError, ValueError):
                    return "goal must be a goal number from mc_goals."
                if not 1 <= number <= len(goals):
                    return f"There is no goal {number} — {fence(player.gamertag)} has {len(goals)}."
                only, params["gid"] = " AND goal_id = :gid", goals[number - 1].id
            rows = (
                await s.execute(
                    text(
                        "SELECT at, text, source, goal_id FROM app.mc_goal_log"
                        f" WHERE xuid = :k{only} ORDER BY at DESC LIMIT :n"
                    ),
                    params,
                )
            ).all()
        if not rows:
            return ToolOutput(f"{fence(player.gamertag)}'s log is empty.", result_brief="empty")
        body = "\n".join(
            f"{_when(r.at)} [{r.source}]"
            + (f" (goal {number_of[r.goal_id]})" if r.goal_id in number_of else "")
            + f" {r.text}"
            for r in reversed(rows)
        )
        return ToolOutput(
            f"Progress log for {fence(player.gamertag)}, oldest first:\n{fence(body)}",
            result_brief=f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}",
        )

    # --- memory: line-level only, history kept -----------------------------------------

    async def mc_memory_read(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        async with scoped_session(maker, ctx.session) as s:
            player = await chat_player(s, ctx)
            if player is None:
                return no_player
            lines = await _current_lines(s, player.key)
        if not lines:
            return ToolOutput(
                f"Nothing remembered about {fence(player.gamertag)} yet.", result_brief="empty"
            )
        body = "\n".join(f"{i}. {t}" for i, (_, t) in enumerate(lines, start=1))
        return ToolOutput(
            f"Memory for {fence(player.gamertag)}:\n{fence(body)}",
            result_brief=f"{len(lines)} line(s)",
        )

    def memory_edit(op: str) -> Callable[[dict, ToolContext], Awaitable[str | ToolOutput]]:
        async def run(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
            new_text = " ".join(str(arguments.get("text") or "").split())
            old_text = " ".join(str(arguments.get("old_text") or "").split())
            try:
                line = int(arguments.get("line") or 0)
            except (TypeError, ValueError):
                return "line must be a line number from mc_memory_read."
            async with scoped_session(maker, ctx.session) as s:
                player = await chat_player(s, ctx)
                if player is None:
                    return no_player
                current = await _current_lines(s, player.key)
                refusal = check_memory_op([t for _, t in current], op, line, new_text)
                # replace and remove name the line's CURRENT text as well as its number:
                # numbers shift after a remove, and a stale one must never hit another line.
                expect = new_text if op == "remove" else old_text
                if op == "replace" and not old_text and not refusal:
                    refusal = "old_text is required — the line's current wording, verbatim."
                if (
                    op != "add"
                    and not refusal
                    and current[line - 1][1].casefold() != expect.casefold()
                ):
                    refusal = (
                        f"line {line} reads {fence(current[line - 1][1])}, not what you passed."
                        " Read the memory again and use that line's current number and text."
                    )
                if refusal:
                    return ToolOutput(f"Nothing changed: {refusal}", result_brief="refused")
                if op in ("replace", "remove"):
                    seq, before = current[line - 1]
                    await s.execute(
                        text(
                            "UPDATE app.mc_player_memory SET superseded_at = now()"
                            " WHERE xuid = :k AND seq = :seq AND superseded_at IS NULL"
                        ),
                        {"k": player.key, "seq": seq},
                    )
                else:
                    seq = max((q for q, _ in current), default=0) + 1
                    before = ""
                if op in ("add", "replace"):
                    await s.execute(
                        text(
                            "INSERT INTO app.mc_player_memory (xuid, seq, text, source)"
                            " VALUES (:k, :seq, :t, 'owner')"
                        ),
                        {"k": player.key, "seq": seq, "t": new_text},
                    )
                count = len(current) + (1 if op == "add" else -1 if op == "remove" else 0)
            # The receipt quotes before and after, so the model's account of its own memory
            # stays grounded (the archivist's lesson).
            receipt = {
                "add": f"Added line {count}: {fence(new_text)}.",
                "replace": f"Line {line} was {fence(before)}, now {fence(new_text)}"
                " (the old text is kept).",
                "remove": f"Removed line {line}, {fence(before)} (kept in history; lines after"
                " it move up one).",
            }[op]
            return ToolOutput(
                f"{receipt} {fence(player.gamertag)}'s memory now has {count} line(s).",
                result_brief=f"{op} · {count} line(s)",
            )

        return run

    handlers: dict[str, ToolHandler] = {
        "mc_player": mc_player,
        "mc_server_status": mc_server_status,
        "mc_players": mc_players,
        "mc_play_history": mc_play_history,
        "mc_world_info": mc_world_info,
        "mc_locate": mc_locate,
        "mc_what_is_at": mc_what_is_at,
        "mc_nearby": mc_nearby,
        "mc_find_block": mc_find_block,
        "mc_find_mob": mc_find_mob,
        "mc_find_item": mc_find_item,
        "mc_find_villager": mc_find_villager,
        "mc_find_pet": mc_find_pet,
        "mc_set_waypoint": mc_set_waypoint,
        "mc_tricorder_scan": mc_tricorder_scan,
        "mc_place_save": mc_place_save,
        "mc_places": mc_places,
        "mc_place_remove": mc_place_remove,
        "mc_goals": mc_goals,
        "mc_goal_create": mc_goal_create,
        "mc_goal_update": mc_goal_update,
        "mc_goal_progress": mc_goal_progress,
        "mc_log": mc_log,
        "mc_log_read": mc_log_read,
        "mc_memory_read": mc_memory_read,
        "mc_memory_add": memory_edit("add"),
        "mc_memory_replace": memory_edit("replace"),
        "mc_memory_remove": memory_edit("remove"),
    }
    return {name: _guarded(name, h) for name, h in handlers.items()}


def _guarded(name: str, handler: ToolHandler) -> ToolHandler:
    """Every failure comes back as a sentence the model can act on, never the loop's
    generic internal error — and says plainly that nothing was written."""

    async def run(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        try:
            return await handler(arguments, ctx)
        except Exception as exc:  # noqa: BLE001 — a failed Minecraft tool is an observation
            log.warning("minecraft.tool_failed", tool=name, error=repr(exc))
            return f"{name} failed (internal) — nothing was changed. Say so; don't guess."

    return run
