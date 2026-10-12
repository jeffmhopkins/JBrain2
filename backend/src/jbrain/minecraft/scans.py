"""The deep-scan limit: two searches per player per Minecraft day, in game only.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M17 (owner, 2026-10-11). In the story the
satellite's deep scan has to recharge (Bleepbloop_City `notes/lore.md`, "The deep scan").

- **What counts**: the searches that find something nobody pointed out — `mc_locate`,
  `mc_nearby`, `mc_find_block`, and `mc_find_item` past the Tricorder's 48-block
  short-range scan (§M23). Everything else is free.
- **In game only**: the handlers gate a scan only while a door turn's asker is set
  (`minecraft/asker.py`), the same signal `mc_show_map` refuses on. The owner's PWA chats
  never reach this module.
- **The day** is the world's own counter (`time query day`), so sleeping starts a new one.
- **A repeat is free**: the same search from within `REPEAT_RADIUS` blocks of one already
  paid for today answers from the stored result.
- **Per player per world folder**: nothing is shared between players.

Charged by reserving a scan before the search and refunding it if the search didn't run,
so two calls in one turn can never both spend the last scan.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.loop import ToolOutput
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft.asker import Asker

SCANS_PER_DAY = 2
# "About the same place": well inside a structure's spacing, so a repeat from here can't
# be a different village, and a short walk doesn't cost a second scan.
REPEAT_RADIUS = 256
# The canon line, word for word (lore.md, "The deep scan", signed off 2026-10-11).
RECHARGE = "…the deep scan needs to recharge, crew. Ask me again tomorrow."
# The day a scan counts against when the world's day has never been read: its own bucket,
# replaced as soon as a real day is seen.
UNKNOWN_DAY = -1

_DAY = re.compile(r"\bDay is (\d+)\b")
_OWNER = SessionContext(principal_kind="owner")


def parse_day(lines: list[str] | str) -> int | None:
    """The day number out of `time query day`'s reply, or None (a down server's sentence,
    or a reply with no day in it)."""
    if isinstance(lines, str):
        return None
    for line in lines:
        if m := _DAY.search(line):
            return int(m.group(1))
    return None


def scans_left(n: int) -> str:
    return f"{n} scan{'' if n == 1 else 's'} left today"


@dataclass(frozen=True)
class Search:
    """One deep scan as asked: which tool, what it looks for (every setting but the
    position), and where from."""

    tool: str
    what: str
    dim: str
    x: int
    z: int

    def repeats(self, stored: dict[str, Any]) -> bool:
        return (
            stored.get("tool") == self.tool
            and stored.get("what") == self.what
            and stored.get("dim") == self.dim
            and math.dist((self.x, self.z), (int(stored["x"]), int(stored["z"]))) <= REPEAT_RADIUS
        )


class ScanBook(Protocol):
    """Where a world's scan counts live. `SqlScanBook` in the app; a dict in unit tests."""

    async def last_day(self, world: str) -> int | None: ...

    async def today(self, world: str, xuid: str, day: int) -> tuple[int, list[dict[str, Any]]]:
        """Scans used and results stored for this player and day."""
        ...

    async def reserve(self, world: str, xuid: str, gamertag: str, day: int) -> int | None:
        """Spend one scan if any is left: the new count used, else None. Drops the world's
        other days."""
        ...

    async def refund(self, world: str, xuid: str, day: int) -> None: ...

    async def remember(self, world: str, xuid: str, day: int, result: dict[str, Any]) -> None: ...


async def scan_day(
    book: ScanBook, asker: Asker, day_reply: list[str] | str, *, needs_server: bool
) -> int | str:
    """The day a scan counts against, or why it can't run. A search that needs the
    running server refuses when the day can't be read (the search would fail anyway); one
    that reads the world's files counts against the last day this world was seen on."""
    day = parse_day(day_reply)
    if day is not None:
        return day
    if needs_server:
        return (
            "The Minecraft server didn't say what day it is in the world, so the deep scan"
            " can't run right now and nothing was searched. Try again in a minute."
        )
    last = await book.last_day(asker.world)
    return UNKNOWN_DAY if last is None else last


Run = Callable[[], Awaitable[tuple[str, bool]]]


async def deep_scan(book: ScanBook, asker: Asker, day: int, search: Search, run: Run) -> str:
    """Run `search` for the in-game asker within today's limit. `run` does the search and
    says whether it really searched (a bad id or a down server is refunded)."""
    used, results = await book.today(asker.world, asker.key, day)
    stored = next((r for r in results if search.repeats(r)), None)
    if stored is not None:
        return ToolOutput(
            f"{stored['text']}\n(Dave found this for the player earlier today, from"
            f" ({stored['x']}, {stored['z']}). A repeat is free:"
            f" {scans_left(max(SCANS_PER_DAY - used, 0))}.)",
            result_brief="repeat, free",
        )
    spent = await book.reserve(asker.world, asker.key, asker.gamertag, day)
    if spent is None:
        return ToolOutput(
            f"Out of deep scans: both of today's {SCANS_PER_DAY} are used (Minecraft day"
            f" {day}), so nothing was searched. A new day, and sleeping through the night"
            f" counts, brings {SCANS_PER_DAY} more; repeats of today's finds and everything"
            " that isn't a search stay free. Answer with Dave's line, word for word:"
            f" {RECHARGE}",
            result_brief="out of scans",
        )
    try:
        out, searched = await run()
    except BaseException:
        await book.refund(asker.world, asker.key, day)
        raise
    if not searched:
        await book.refund(asker.world, asker.key, day)
        return out
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
            "text": str(out),
        },
    )
    left = scans_left(SCANS_PER_DAY - spent)
    brief = getattr(out, "result_brief", "") or search.tool
    return ToolOutput(
        f"{out}\nThat used a deep scan: {left} (say so).", result_brief=f"{brief} · {left}"
    )


class SqlScanBook:
    """`app.mc_scans` under the turn's own RLS context (owner-only, migration 0229)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> None:
        self._maker = maker
        self._ctx = ctx

    async def last_day(self, world: str) -> int | None:
        async with scoped_session(self._maker, self._ctx) as s:
            return (
                await s.execute(
                    text(
                        "SELECT mc_day FROM app.mc_scans WHERE world = :w"
                        " ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"w": world},
                )
            ).scalar()

    async def today(self, world: str, xuid: str, day: int) -> tuple[int, list[dict[str, Any]]]:
        async with scoped_session(self._maker, self._ctx) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT used, results FROM app.mc_scans"
                        " WHERE world = :w AND xuid = :k AND mc_day = :d"
                    ),
                    {"w": world, "k": xuid, "d": day},
                )
            ).first()
        return (0, []) if row is None else (int(row.used), list(row.results))

    async def reserve(self, world: str, xuid: str, gamertag: str, day: int) -> int | None:
        async with scoped_session(self._maker, self._ctx) as s:
            # Only today matters, so every other day in this world goes: a world whose
            # clock was set back starts clean rather than inheriting a stale count.
            await s.execute(
                text("DELETE FROM app.mc_scans WHERE world = :w AND mc_day <> :d"),
                {"w": world, "d": day},
            )
            return (
                await s.execute(
                    text(
                        "INSERT INTO app.mc_scans (world, xuid, gamertag, mc_day, used)"
                        " VALUES (:w, :k, :g, :d, 1)"
                        " ON CONFLICT (world, xuid, mc_day) DO UPDATE"
                        " SET used = app.mc_scans.used + 1, gamertag = excluded.gamertag,"
                        " updated_at = now() WHERE app.mc_scans.used < :max RETURNING used"
                    ),
                    {"w": world, "k": xuid, "g": gamertag, "d": day, "max": SCANS_PER_DAY},
                )
            ).scalar()

    async def refund(self, world: str, xuid: str, day: int) -> None:
        async with scoped_session(self._maker, self._ctx) as s:
            await s.execute(
                text(
                    "UPDATE app.mc_scans SET used = greatest(used - 1, 0)"
                    " WHERE world = :w AND xuid = :k AND mc_day = :d"
                ),
                {"w": world, "k": xuid, "d": day},
            )

    async def remember(self, world: str, xuid: str, day: int, result: dict[str, Any]) -> None:
        async with scoped_session(self._maker, self._ctx) as s:
            await s.execute(
                text(
                    "UPDATE app.mc_scans"
                    " SET results = results || jsonb_build_array(CAST(:r AS jsonb))"
                    " WHERE world = :w AND xuid = :k AND mc_day = :d"
                ),
                {"w": world, "k": xuid, "d": day, "r": json.dumps(result)},
            )


async def summary(maker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Today's counts per world and player, for the debug API: no search results leave."""
    async with scoped_session(maker, _OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT world, xuid, gamertag, mc_day, used, jsonb_array_length(results)"
                    " AS stored, extract(epoch FROM updated_at) AS updated_at"
                    " FROM app.mc_scans ORDER BY world, mc_day DESC, gamertag"
                )
            )
        ).all()
    return [
        {
            "world": r.world,
            "xuid": r.xuid,
            "gamertag": r.gamertag,
            "day": int(r.mc_day),
            "used": int(r.used),
            "left": max(SCANS_PER_DAY - int(r.used), 0),
            "stored_results": int(r.stored),
            "updated_at": float(r.updated_at),
        }
        for r in rows
    ]
