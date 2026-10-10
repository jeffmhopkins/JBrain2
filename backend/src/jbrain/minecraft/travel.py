"""Drain the sidecar's position samples into the travel log (MINECRAFT_BEDROCK_PLAN §T1).

The sidecar samples each online player's position every few seconds (`querytarget` on
the console) and keeps a sample only once they have moved; this loop copies those into
`app.mc_player_track` and widens each player's fog of war in `app.mc_player_explored`.
It runs whether or not anyone has a screen open, so the history M8's maps and M8a's
timeline draw is already there when they arrive.

Boot handling mirrors the play-session drain: samples are numbered per sidecar boot,
a new boot resets the cursor, and replaying a sample writes nothing twice.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft.sessions import player_key

log = structlog.get_logger(__name__)

POLL_SECONDS = 10.0
# A chunk is "explored" by a player who came within this many chunks of it — about what
# they could see at a modest view distance, so the fog clears where they looked.
REVEAL_CHUNKS = 4
_OWNER = SessionContext(principal_kind="owner")

INSERT_SAMPLE = text(
    """
    INSERT INTO app.mc_player_track
        (world, xuid, gamertag, boot_id, sample_id, at, dim, x, y, z, yaw)
    SELECT :world, :xuid, :name, :boot, :sid, :at, :dim, :x, :y, :z, :yaw
     WHERE NOT EXISTS (
         -- A sample of terrain a reset or import has since replaced must not come back
         -- just because it drained after the replacement did.
         SELECT 1 FROM app.mc_player_events
          WHERE world = :world AND kind = 'world_replaced' AND at > :at)
    ON CONFLICT (boot_id, sample_id) DO NOTHING
    RETURNING id
    """
)

REVEAL = text(
    """
    INSERT INTO app.mc_player_explored (world, xuid, dim, cx, cz, first_seen, last_seen)
    SELECT :world, :xuid, :dim, CAST(:cx AS integer) + dx, CAST(:cz AS integer) + dz,
           :at, :at
      FROM generate_series(-CAST(:r AS integer), CAST(:r AS integer)) AS dx,
           generate_series(-CAST(:r AS integer), CAST(:r AS integer)) AS dz
    ON CONFLICT (world, xuid, dim, cx, cz) DO UPDATE
       SET first_seen = least(app.mc_player_explored.first_seen, excluded.first_seen),
           last_seen = greatest(app.mc_player_explored.last_seen, excluded.last_seen)
    """
)


def chunk_of(coord: float) -> int:
    return math.floor(coord / 16)


class TrackDrain:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: Any) -> None:
        self._maker = maker
        self._settings = settings
        self.boot_id: str | None = None
        self.cursor = 0

    async def tick(self) -> int:
        """One poll; returns how many new samples were stored. A stopped or unreachable
        sidecar is a quiet no-op."""
        try:
            got = await mc.call(self._settings, "GET", "/track", params={"after": self.cursor})
        except HTTPException:
            return 0
        boot = str(got.get("boot_id", ""))
        new_boot = boot != self.boot_id
        if new_boot and self.cursor:
            got = await mc.call(self._settings, "GET", "/track", params={"after": 0})
        samples = list(got.get("samples", []))
        stored = 0
        async with scoped_session(self._maker, _OWNER) as s:
            for sample in samples:
                stored += await self.apply(s, boot, sample)
        if new_boot:
            self.boot_id, self.cursor = boot, 0
        self.cursor = max([self.cursor, *(int(t["id"]) for t in samples)])
        return stored

    @staticmethod
    async def apply(s: AsyncSession, boot: str, t: dict[str, Any]) -> int:
        params = {
            "world": str(t.get("world") or "world"),
            "xuid": player_key(str(t.get("xuid", "")), str(t.get("name", ""))),
            "name": str(t.get("name", "")),
            "boot": boot,
            "sid": int(t["id"]),
            "at": datetime.fromtimestamp(float(t["at"]), UTC),
            "dim": str(t.get("dim") or "overworld"),
            "x": float(t["x"]),
            "y": float(t["y"]),
            "z": float(t["z"]),
            "yaw": float(t["yaw"]) if t.get("yaw") is not None else None,
        }
        if (await s.execute(INSERT_SAMPLE, params)).first() is None:
            return 0  # a replay, or terrain that has since been replaced
        await s.execute(
            REVEAL,
            {
                "world": params["world"],
                "xuid": params["xuid"],
                "dim": params["dim"],
                "cx": chunk_of(params["x"]),
                "cz": chunk_of(params["z"]),
                "at": params["at"],
                "r": REVEAL_CHUNKS,
            },
        )
        return 1


SUMMARY_SQL = text(
    """
    SELECT t.world, t.xuid,
           (array_agg(t.gamertag ORDER BY t.at DESC))[1] AS gamertag,
           count(*) AS samples,
           extract(epoch FROM min(t.at)) AS first_at,
           extract(epoch FROM max(t.at)) AS last_at,
           (SELECT count(*) FROM app.mc_player_explored e
             WHERE e.world = t.world AND e.xuid = t.xuid) AS chunks,
           (SELECT count(*) FROM app.mc_player_events v
             WHERE v.world = t.world AND v.xuid = t.xuid AND v.kind = 'death') AS deaths
      FROM app.mc_player_track t
     GROUP BY t.world, t.xuid
     ORDER BY t.world, samples DESC
    """
)


async def summary(maker: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """What the travel log holds, per world and player: enough to see it recording."""
    async with scoped_session(maker, _OWNER) as s:
        rows = (await s.execute(SUMMARY_SQL)).mappings().all()
    return [
        {
            "world": r["world"],
            "xuid": r["xuid"],
            "gamertag": r["gamertag"],
            "samples": int(r["samples"]),
            "chunks_explored": int(r["chunks"]),
            "deaths": int(r["deaths"]),
            "first_at": float(r["first_at"]),
            "last_at": float(r["last_at"]),
        }
        for r in rows
    ]


async def run_track_drain(drain: TrackDrain, *, interval: float = POLL_SECONDS) -> None:
    """Forever: drain, wait, repeat. Outlives every server restart."""
    while True:
        try:
            await drain.tick()
        except Exception as exc:  # noqa: BLE001 — the loop outlives every failure
            log.warning("minecraft.track_drain_error", error=repr(exc))
        await asyncio.sleep(interval)
