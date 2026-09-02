"""The APRS heard log: drain the sidecar's packet stream into `app.aprs_packets`.

docs/plans/APRS_CONTROL_PLAN.md P1.

**Why a background loop rather than a route the PWA opens.** A log that only records
while someone is looking is not a log. The owner enables APRS logging and drives away;
the point is that what arrived while nobody watched is there afterwards. So this runs
for as long as a logging session holds the tuner, and the screen reads rows out of the
table rather than being the thing that fills it.

**Idle is the normal state.** Most of the time no session is logging and this costs one
cheap status check per tick. It attaches only when a session with `purpose == "aprs"`
appears, and lets go the moment it ends.

**Heard text is untrusted.** These rows came off the air from anyone with a transmitter,
and a source callsign is plain bytes that forge trivially. Nothing here interprets a
packet — it is stored, and the two trust tiers in the plan govern what may read it. A
packet reaching a model as a prompt is prompt injection with an antenna.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

log = structlog.get_logger(__name__)

# How long to wait before looking for a logging session again. A packet channel is not
# urgent and the check is a cheap GET, so this is slow on purpose.
IDLE_POLL_SECONDS = 5.0
# A frame's info field is bounded well under this; the cap exists so a malformed or
# hostile frame cannot write an unbounded row.
MAX_INFO = 512
MAX_RAW = 2048


class AprsLog:
    """Drains one logging session's packets into the table, then waits for the next."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], base_url: str) -> None:
        self._maker = maker
        self._base = base_url

    async def tick(self) -> None:
        """Attach to a logging session if there is one, and drain it until it ends."""
        if not self._base:
            return
        async with httpx.AsyncClient(base_url=self._base, timeout=None) as client:
            if not await self._is_logging(client):
                return
            await self._drain(client)

    async def _is_logging(self, client: httpx.AsyncClient) -> bool:
        try:
            resp = await client.get("/healthz", timeout=5.0)
            health: dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError):
            return False
        session = health.get("listening") or {}
        return bool(session.get("purpose") == "aprs")

    async def _drain(self, client: httpx.AsyncClient) -> None:
        """Read frames until the stream ends, storing each one.

        The stream ending is the normal exit: the owner released the radio, or it was
        retuned. Errors are swallowed to the log for the same reason — this loop
        outlives any one session, and a sidecar restart must not end it."""
        try:
            async with client.stream("GET", "/listen/packets") as upstream:
                if upstream.status_code != 200:
                    return
                async for line in upstream.aiter_lines():
                    row = _parse(line)
                    if row is None:
                        continue
                    await self._store(row)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("aprs_log.stream_ended", error=repr(exc))

    async def _store(self, row: dict[str, Any]) -> None:
        try:
            async with scoped_session(self._maker, SessionContext(principal_kind="owner")) as s:
                await s.execute(
                    text(
                        "INSERT INTO app.aprs_packets"
                        " (frequency_hz, source, destination, path, info, raw)"
                        " VALUES (:hz, :src, :dst, :path, :info, :raw)"
                    ),
                    row,
                )
                await s.commit()
        except Exception as exc:  # noqa: BLE001 — one bad row must not end the log
            log.warning("aprs_log.store_failed", error=repr(exc))


def _parse(line: str) -> dict[str, Any] | None:
    """One newline-framed row from the sidecar, bounded, or None to skip it.

    Every field is treated as hostile: the sidecar decoded it, but what it decoded came
    off a shared channel. Lengths are capped here rather than trusted from the wire."""
    if not line.strip():
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("keepalive"):
        return None
    source = str(payload.get("source") or "")
    info = str(payload.get("info") or "")
    if not source:
        return None  # a frame with no sender is not attributable, so it is not a row
    try:
        hz = int(payload.get("frequency_hz") or 0)
    except (TypeError, ValueError):
        # A crafted frequency must SKIP one row, not end the drain. `_drain` catches
        # ValueError around the whole read loop, so raising here would let a single
        # hostile frame stop the log until the session was restarted.
        return None
    path = payload.get("path")
    return {
        "hz": hz,
        "src": source[:16],
        "dst": str(payload.get("destination") or "")[:16],
        "path": [str(p)[:16] for p in path][:8] if isinstance(path, list) else [],
        "info": info[:MAX_INFO],
        "raw": str(payload.get("raw") or "")[:MAX_RAW],
    }


async def run_aprs_log_loop(logger: AprsLog, *, interval: float = IDLE_POLL_SECONDS) -> None:
    """Forever: attach to a logging session when one exists, drain it, wait, repeat."""
    while True:
        try:
            await logger.tick()
        except Exception as exc:  # noqa: BLE001 — the loop outlives every session
            log.warning("aprs_log.tick_error", error=repr(exc))
        await asyncio.sleep(interval)


# --- reading the log back ------------------------------------------------------------

# What jerv gets by default, and the ceiling however large a limit it asks for. A packet
# channel produces a lot of rows and a turn does not need most of them.
RECENT_DEFAULT = 20
RECENT_MAX = 100


class AprsReader:
    """Reads the heard log back, on the caller's RLS scope.

    A service holding the maker, like `SearchService` and the other readers a tool is
    handed — the scope arrives per call because it belongs to the turn, not to the
    reader. The owner-only policy on the table decides visibility, so a non-owner gets
    an empty list from Postgres rather than a check here (CLAUDE.md rule 3)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker

    async def recent(
        self, ctx: SessionContext, *, limit: int = RECENT_DEFAULT, source: str | None = None
    ) -> list[dict[str, Any]]:
        """The most recently heard packets, newest first."""
        bounded = max(1, min(int(limit), RECENT_MAX))
        where = "WHERE source = :source" if source else ""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT heard_at, frequency_hz, source, destination, path, info"
                        f" FROM app.aprs_packets {where}"
                        " ORDER BY heard_at DESC LIMIT :limit"
                    ),
                    {"limit": bounded, **({"source": source} if source else {})},
                )
            ).mappings()
            return [dict(row) for row in rows]
