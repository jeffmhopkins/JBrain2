"""The Tricorder's live scan each player has on, as the map and Dave see it (plan §M25,
`app.mc_tricorder_scans`, migration 0244).

The pack sends a `scan` line when a scan starts (`set`), when what it finds changes and
once a minute while it doesn't (`found` / `none`), and when it ends (`stop`), and a `scans`
snapshot of every kept scan after each load; the session drain (`sessions.SessionDrain`)
hands each to `apply`, in order, inside its own transaction. The waypoint under a scan is
`targets.py`'s row and is never touched here. A reset or import of the world clears the
rows; re-reading events after an api restart replays them in order, so the rows end where
the last one left them.

Named `livescan`, not `scans`: `scans.py` is the deep-scan limit, a different thing.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

_OWNER = SessionContext(principal_kind="owner")

KINDS = ("scan", "scans")
# The pack's SCAN_REACH: a 33 × 33 × 33 box round the player, its y cut at the world's
# floor and ceiling (rules.js `scanBox`).
REACH = 16
# Each dimension's lowest and highest block (heightRange's max is exclusive in game).
HEIGHT = {"overworld": (-64, 319), "nether": (0, 127), "the_end": (0, 255)}

_UPSERT = text(
    """
    INSERT INTO app.mc_tricorder_scans (world, player, gamertag, label, ids, state,
        dim, x, y, z, set_at, scanned_at, seen_at)
    VALUES (:w, :player, :gamertag, :label, CAST(:ids AS text[]), :state,
        :dim, :x, :y, :z, :set_at, :scanned_at, :at)
    ON CONFLICT (world, player) DO UPDATE
       SET gamertag = excluded.gamertag,
           label = CASE WHEN excluded.label <> '' THEN excluded.label
                        ELSE app.mc_tricorder_scans.label END,
           ids = CASE WHEN cardinality(excluded.ids) > 0 THEN excluded.ids
                      ELSE app.mc_tricorder_scans.ids END,
           state = excluded.state, dim = excluded.dim, x = excluded.x, y = excluded.y,
           z = excluded.z,
           set_at = coalesce(excluded.set_at, app.mc_tricorder_scans.set_at),
           scanned_at = excluded.scanned_at,
           seen_at = excluded.seen_at
    """
)

# A snapshot names the scan, not what it last found: a row whose scan is unchanged keeps
# its result until the first scan after the load sends a new one; a new scan starts empty.
_KEEP = text(
    """
    INSERT INTO app.mc_tricorder_scans (world, player, gamertag, label, ids, state, seen_at)
    VALUES (:w, :player, :gamertag, :label, CAST(:ids AS text[]), 'set', :at)
    ON CONFLICT (world, player) DO UPDATE
       SET gamertag = excluded.gamertag, seen_at = excluded.seen_at,
           label = excluded.label, ids = excluded.ids,
           state = CASE WHEN app.mc_tricorder_scans.label = excluded.label
                        THEN app.mc_tricorder_scans.state ELSE 'set' END,
           dim = CASE WHEN app.mc_tricorder_scans.label = excluded.label
                      THEN app.mc_tricorder_scans.dim END,
           x = CASE WHEN app.mc_tricorder_scans.label = excluded.label
                    THEN app.mc_tricorder_scans.x END,
           y = CASE WHEN app.mc_tricorder_scans.label = excluded.label
                    THEN app.mc_tricorder_scans.y END,
           z = CASE WHEN app.mc_tricorder_scans.label = excluded.label
                    THEN app.mc_tricorder_scans.z END,
           scanned_at = CASE WHEN app.mc_tricorder_scans.label = excluded.label
                             THEN app.mc_tricorder_scans.scanned_at END
    """
)

_FOR_WORLD = text(
    """
    SELECT player, gamertag, label, ids, state, dim, x, y, z,
           extract(epoch FROM set_at) AS set_at, extract(epoch FROM scanned_at) AS scanned_at
      FROM app.mc_tricorder_scans WHERE world = :w
    """
)


def key(gamertag: str) -> str:
    return gamertag.strip().lower()


def y_range(dim: str, y: float) -> tuple[int, int]:
    """The heights one scan reads for a player standing at `y`: 16 blocks each way, cut at
    the dimension's floor and ceiling (never shifted), as the pack's `scanBox` does."""
    lo, hi = HEIGHT.get(dim, HEIGHT["overworld"])
    at = math.floor(y)
    return max(lo, min(at - REACH, hi)), max(lo, min(at + REACH, hi))


def _ids(raw: Any) -> list[str]:
    return [str(i) for i in raw] if isinstance(raw, list) else []


async def _row(
    s: AsyncSession,
    world: str,
    e: dict[str, Any],
    at: datetime,
    *,
    state: str,
    started: bool = False,
) -> None:
    gamertag = str(e["player"])
    found = state == "found"
    await s.execute(
        _UPSERT,
        {
            "w": world,
            "player": key(gamertag),
            "gamertag": gamertag,
            "label": str(e.get("label") or ""),
            "ids": _ids(e.get("ids")),
            "state": state,
            "dim": str(e["dim"]) if found else None,
            "x": int(e["x"]) if found else None,
            "y": int(e["y"]) if found else None,
            "z": int(e["z"]) if found else None,
            "set_at": at if started else None,
            "scanned_at": None if started else at,
            "at": at,
        },
    )


async def apply(s: AsyncSession, e: dict[str, Any], at: datetime) -> None:
    """One drained event: a scan change, a snapshot, or a world replaced."""
    kind = e.get("kind")
    world = str(e.get("world") or "world")
    if kind == "world_replaced":
        # Its players' scans were the old world's properties, gone with it. The new
        # world's snapshot, drained after this, fills them again.
        await s.execute(
            text("DELETE FROM app.mc_tricorder_scans WHERE world = :w AND seen_at <= :at"),
            {"w": world, "at": at},
        )
        return
    if kind not in KINDS:
        return
    try:
        await _apply(s, kind, world, e, at)
    except (KeyError, TypeError, ValueError):
        # The sidecar checked every field; a line it let through that still isn't whole is
        # dropped rather than stopping the drain behind it.
        return


async def _apply(s: AsyncSession, kind: str, world: str, e: dict[str, Any], at: datetime) -> None:
    if kind == "scan":
        op = e.get("op")
        if op == "stop":
            await s.execute(
                text("DELETE FROM app.mc_tricorder_scans WHERE world = :w AND player = :p"),
                {"w": world, "p": key(str(e["player"]))},
            )
        elif op == "set":
            await _row(s, world, e, at, state="set", started=True)
        elif op in ("found", "none"):
            await _row(s, world, e, at, state=str(op))
        return
    items = [i for i in e.get("all") or [] if isinstance(i, dict) and i.get("player")]
    if not e.get("truncated"):
        await s.execute(
            text(
                "DELETE FROM app.mc_tricorder_scans WHERE world = :w"
                " AND NOT (player = ANY(CAST(:keep AS text[])))"
            ),
            {"w": world, "keep": [key(str(i["player"])) for i in items]},
        )
    for i in items:
        gamertag = str(i["player"])
        await s.execute(
            _KEEP,
            {
                "w": world,
                "player": key(gamertag),
                "gamertag": gamertag,
                "label": str(i.get("label") or ""),
                "ids": _ids(i.get("ids")),
                "at": at,
            },
        )


async def read(s: AsyncSession, world: str) -> dict[str, dict[str, Any]]:
    """Every scan on in one world folder, by lowercased gamertag: its label, what the last
    scan found (`found` None for nothing in reach or nothing heard yet), and when the last
    scan was heard (`at`, epoch seconds; None before the first)."""
    rows = (await s.execute(_FOR_WORLD, {"w": world})).mappings().all()
    return {
        str(r["player"]): {
            "gamertag": r["gamertag"],
            "label": r["label"],
            "ids": list(r["ids"] or []),
            "state": r["state"],
            "found": (
                {"dim": r["dim"], "x": int(r["x"]), "y": int(r["y"]), "z": int(r["z"])}
                if r["state"] == "found"
                else None
            ),
            "at": None if r["scanned_at"] is None else float(r["scanned_at"]),
            "set_at": None if r["set_at"] is None else float(r["set_at"]),
        }
        for r in rows
    }


async def for_world(
    maker: async_sessionmaker[AsyncSession], world: str, ctx: SessionContext = _OWNER
) -> dict[str, dict[str, Any]]:
    """`read` in its own session, for the map route; a share passes its pinned `ctx`."""
    async with scoped_session(maker, ctx) as s:
        return await read(s, world)
