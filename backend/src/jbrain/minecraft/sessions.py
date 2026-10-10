"""Drain the Minecraft sidecar's join/leave events into `app.mc_player_sessions`.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M1. The same shape as the APRS heard log
(`sdr/aprslog.py`): a background loop in the api, so play time is recorded whether or
not anyone has the Ops screen open, and the screen reads rows rather than being what
fills them.

The sidecar numbers its events per BOOT (`boot_id`), so a container restart is visible
as a new boot: the cursor resets, and any session the old boot left open (a crash logs
no leaves) closes at its `last_seen_at`, the heartbeat this loop keeps on open rows.
Re-reading events is harmless, because a join inserts on a unique `(boot_id,
start_event_id)` and a leave closes only sessions that began before it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc

log = structlog.get_logger(__name__)

POLL_SECONDS = 5.0
_OWNER = SessionContext(principal_kind="owner")


def player_key(xuid: str, name: str) -> str:
    # online-mode is on, so every real player has an xuid; the fallback keeps a row
    # attributable if one ever arrives without.
    return xuid or f"name:{name}"


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


class SessionDrain:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: Any) -> None:
        self._maker = maker
        self._settings = settings
        self.boot_id: str | None = None
        self.cursor = 0

    async def tick(self) -> int:
        """One poll: apply new events, then heartbeat open sessions. Returns how many
        events were applied. A stopped or unreachable sidecar is a quiet no-op."""
        try:
            got = await mc.call(self._settings, "GET", "/events", params={"after": self.cursor})
        except HTTPException:
            return 0
        boot = str(got.get("boot_id", ""))
        new_boot = boot != self.boot_id
        if new_boot and self.cursor:
            # The sidecar restarted and numbers from 1 again: re-read this boot whole.
            got = await mc.call(self._settings, "GET", "/events", params={"after": 0})
        events = list(got.get("events", []))
        async with scoped_session(self._maker, _OWNER) as s:
            if new_boot:
                # Whatever an earlier boot left open was cut off without leaves.
                await self._close_other_boots(s, boot)
            for e in events:
                await self.apply(s, boot, e)
            await s.execute(
                text(
                    "UPDATE app.mc_player_sessions SET last_seen_at = now()"
                    " WHERE ended_at IS NULL AND boot_id = :boot"
                ),
                {"boot": boot},
            )
        # Only once the transaction has committed: a failed write re-reads these events,
        # and a failed boot change is retried (closing the old boot's sessions) next tick.
        if new_boot:
            self.boot_id, self.cursor = boot, 0
        self.cursor = max([self.cursor, *(int(e["id"]) for e in events)])
        return len(events)

    async def _close_other_boots(self, s: AsyncSession, boot: str) -> None:
        await s.execute(
            text(
                "UPDATE app.mc_player_sessions SET ended_at = last_seen_at"
                " WHERE ended_at IS NULL AND boot_id <> :boot"
            ),
            {"boot": boot},
        )

    @staticmethod
    async def apply(s: AsyncSession, boot: str, e: dict[str, Any]) -> None:
        at = _ts(float(e["at"]))
        kind = e.get("kind")
        if kind == "join":
            key = player_key(str(e.get("xuid", "")), str(e.get("name", "")))
            # A join with no leave before it (a dropped connection BDS never logged)
            # ends the earlier session where the new one begins.
            await s.execute(
                text(
                    "UPDATE app.mc_player_sessions SET ended_at = :at, last_seen_at = :at"
                    " WHERE xuid = :key AND ended_at IS NULL AND started_at < :at"
                ),
                {"key": key, "at": at},
            )
            await s.execute(
                text(
                    "INSERT INTO app.mc_player_sessions"
                    " (xuid, gamertag, boot_id, start_event_id, started_at, last_seen_at)"
                    " VALUES (:key, :name, :boot, :eid, :at, :at)"
                    " ON CONFLICT (boot_id, start_event_id) DO NOTHING"
                ),
                {
                    "key": key,
                    "name": e.get("name", ""),
                    "boot": boot,
                    "eid": int(e["id"]),
                    "at": at,
                },
            )
        elif kind == "leave":
            key = player_key(str(e.get("xuid", "")), str(e.get("name", "")))
            await s.execute(
                text(
                    "UPDATE app.mc_player_sessions SET ended_at = :at, last_seen_at = :at"
                    " WHERE ended_at IS NULL AND started_at <= :at"
                    " AND (xuid = :key OR gamertag = :name)"
                ),
                {"key": key, "name": e.get("name", ""), "at": at},
            )
        elif kind == "server_stop":
            await s.execute(
                text(
                    "UPDATE app.mc_player_sessions SET ended_at = :at, last_seen_at = :at"
                    " WHERE ended_at IS NULL AND boot_id = :boot AND started_at <= :at"
                ),
                {"boot": boot, "at": at},
            )


PLAYERS_SQL = text(
    """
    SELECT xuid,
           (array_agg(gamertag ORDER BY started_at DESC))[1] AS gamertag,
           count(*) AS sessions,
           extract(epoch FROM sum(
               CASE WHEN ended_at IS NULL AND xuid = ANY(:online) THEN now()
                    ELSE coalesce(ended_at, last_seen_at) END - started_at)) AS total_seconds,
           extract(epoch FROM min(started_at)) AS first_seen,
           extract(epoch FROM max(coalesce(ended_at, last_seen_at))) AS last_seen
      FROM app.mc_player_sessions
     GROUP BY xuid
     ORDER BY total_seconds DESC
    """
)


async def players(
    maker: async_sessionmaker[AsyncSession], online: dict[str, float]
) -> list[dict[str, Any]]:
    """Per-player totals. `online` maps xuid → join time from the live server, which
    is the authority on who is on right now; the table is the authority on history."""
    async with scoped_session(maker, _OWNER) as s:
        rows = (await s.execute(PLAYERS_SQL, {"online": list(online)})).mappings().all()
    now = datetime.now(UTC).timestamp()
    out = []
    for r in rows:
        live = r["xuid"] in online
        out.append(
            {
                "xuid": r["xuid"],
                "gamertag": r["gamertag"],
                "online": live,
                "session_started_at": online.get(r["xuid"]) if live else None,
                "total_seconds": round(float(r["total_seconds"] or 0)),
                "sessions": int(r["sessions"]),
                "first_seen": float(r["first_seen"]),
                "last_seen": now if live else float(r["last_seen"]),
            }
        )
    return out


async def run_session_drain(drain: SessionDrain, *, interval: float = POLL_SECONDS) -> None:
    """Forever: drain, wait, repeat. Outlives every server restart."""
    while True:
        try:
            await drain.tick()
        except Exception as exc:  # noqa: BLE001 — the loop outlives every failure
            log.warning("minecraft.session_drain_error", error=repr(exc))
        await asyncio.sleep(interval)
