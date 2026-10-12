"""The Tricorder target each player is following (plan §M21, `app.mc_tricorder_targets`).

Dave's `mc_set_waypoint` writes the target here as well as into the world (the pack keeps
its own copy, which nothing outside the game can read), so the map can draw it. The track
drain calls `arrived` for every new position sample: a player who has reached their target
loses it, here and, through the drain, on their Tricorder.

Rows are keyed by the lowercased gamertag, the pack's own key, because a waypoint can be
set for a gamertag the travel log has never sampled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft.asker import Spot

# Positions are sampled every 10 s and kept only after 4 blocks of movement, so the last
# sample before a player stops can be that far short of where they stand: "arrived" is
# within the Tricorder's own 3 blocks plus that slack.
ARRIVE_BLOCKS = 7.0
# A target with a depth (an ore) also needs the player down at it, not standing above.
ARRIVE_DEPTH = 6.0

_OWNER = SessionContext(principal_kind="owner")

UPSERT = text(
    """
    INSERT INTO app.mc_tricorder_targets (world, player, gamertag, dim, x, y, z, label, set_at)
    VALUES (:world, :player, :gamertag, :dim, :x, :y, :z, :label, now())
    ON CONFLICT (world, player) DO UPDATE
       SET gamertag = excluded.gamertag, dim = excluded.dim, x = excluded.x,
           y = excluded.y, z = excluded.z, label = excluded.label, set_at = excluded.set_at
    """
)

CLEAR = text("DELETE FROM app.mc_tricorder_targets WHERE world = :world AND player = :player")

# Only a sample taken after the target was set can arrive at it: a drain catching up on
# older samples must not clear a target the player hasn't walked to yet.
ARRIVED = text(
    """
    DELETE FROM app.mc_tricorder_targets
     WHERE world = :world AND player = :player AND dim = :dim AND set_at <= :at
       AND sqrt((x + 0.5 - :x) ^ 2 + (z + 0.5 - :z) ^ 2) <= :reach
       AND (y IS NULL OR abs(y - :y) <= :depth)
    RETURNING gamertag
    """
)

FOR_WORLD = text(
    """
    SELECT player, gamertag, dim, x, y, z, label, extract(epoch FROM set_at) AS set_at
      FROM app.mc_tricorder_targets WHERE world = :world
    """
)


class TargetBook(Protocol):
    """Where Dave's tool records a target; unit tests keep it in memory."""

    async def set(self, world: str, gamertag: str, spot: Spot, label: str) -> None: ...

    async def clear(self, world: str, gamertag: str) -> None: ...


class SqlTargetBook:
    def __init__(self, maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> None:
        self._maker = maker
        self._ctx = ctx

    async def set(self, world: str, gamertag: str, spot: Spot, label: str) -> None:
        async with scoped_session(self._maker, self._ctx) as s:
            await set_target(s, world, gamertag, spot, label)

    async def clear(self, world: str, gamertag: str) -> None:
        async with scoped_session(self._maker, self._ctx) as s:
            await clear_target(s, world, gamertag)


def key(gamertag: str) -> str:
    return gamertag.strip().lower()


async def set_target(s: AsyncSession, world: str, gamertag: str, spot: Spot, label: str) -> None:
    await s.execute(
        UPSERT,
        {
            "world": world,
            "player": key(gamertag),
            "gamertag": gamertag,
            "dim": spot.dim,
            "x": spot.x,
            "y": spot.y,
            "z": spot.z,
            "label": label,
        },
    )


async def clear_target(s: AsyncSession, world: str, gamertag: str) -> None:
    await s.execute(CLEAR, {"world": world, "player": key(gamertag)})


async def arrived(
    s: AsyncSession,
    *,
    world: str,
    gamertag: str,
    dim: str,
    x: float,
    y: float,
    z: float,
    at: datetime,
) -> str | None:
    """Drop the player's target when this sample has reached it; the gamertag whose
    Tricorder should be cleared, or None."""
    row = (
        await s.execute(
            ARRIVED,
            {
                "world": world,
                "player": key(gamertag),
                "dim": dim,
                "x": x,
                "y": y,
                "z": z,
                "at": at,
                "reach": ARRIVE_BLOCKS,
                "depth": ARRIVE_DEPTH,
            },
        )
    ).first()
    return str(row.gamertag) if row else None


async def for_world(
    maker: async_sessionmaker[AsyncSession], world: str, ctx: SessionContext = _OWNER
) -> dict[str, dict[str, Any]]:
    """Every current target in one world folder, by lowercased gamertag."""
    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(FOR_WORLD, {"world": world})).mappings().all()
    return {
        str(r["player"]): {
            "dim": r["dim"],
            "x": int(r["x"]),
            "y": None if r["y"] is None else int(r["y"]),
            "z": int(r["z"]),
            "label": r["label"],
            "set_at": float(r["set_at"]),
        }
        for r in rows
    }
