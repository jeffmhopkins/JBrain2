"""The GrowBot diary (migration 0262): the creature's own brain log, rows kept once.

Upstream's page posts a snapshot of its rolling diary every few seconds, so most of each post
is rows the box already has; a row is keyed by the page's diary id, its own timestamp and a
hash of its body, and a repeat is dropped by the database. The rows are the page's, redacted
by the page before they leave it (human speech hashed); the box only bounds their size.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

KEEP: Final = timedelta(days=7)
_PRUNE_EVERY: Final = 200
# Upstream caps a snapshot at 60 KB (`_LOG_POST`) and holds at most 400 rows, so a row or a
# batch past these is not the creature's diary.
ROW_MAX: Final = 16 * 1024
ROWS_MAX: Final = 500
HEAD_MAX: Final = 2 * 1024


@dataclass(frozen=True)
class DiaryBatch:
    sid: str
    rows: list[dict[str, Any]]
    persona_id: str | None = None
    robot: str = ""
    head: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiaryRow:
    id: int
    created_at: datetime
    persona_id: str | None
    robot: str
    sid: str
    t: int
    row: dict[str, Any]
    head: dict[str, Any]


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_t(row: dict[str, Any]) -> int | None:
    t = row.get("t")
    if type(t) is int:
        return t
    if type(t) is float and t == t and abs(t) < 2**53:
        return int(t)
    return None


class Journal(Protocol):
    async def record(self, ctx: SessionContext, batch: DiaryBatch) -> int: ...

    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 500
    ) -> list[DiaryRow]: ...


class SqlJournal:
    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker
        self._writes = 0

    async def record(self, ctx: SessionContext, batch: DiaryBatch) -> int:
        """Keep the rows not seen before; returns how many were new. A row without a numeric
        `t` or past ROW_MAX is skipped, never the batch."""
        ts: list[int] = []
        hashes: list[str] = []
        events: list[str] = []
        bodies: list[str] = []
        head = _dump(batch.head)
        if len(head) > HEAD_MAX:
            head = "{}"
        for row in batch.rows[:ROWS_MAX]:
            t = row_t(row) if isinstance(row, dict) else None
            if t is None:
                continue
            body = _dump(row)
            if len(body) > ROW_MAX:
                continue
            event = row.get("e")
            ts.append(t)
            hashes.append(hashlib.sha256(body.encode()).hexdigest()[:32])
            events.append(event[:80] if isinstance(event, str) else "")
            bodies.append(body)
        if not ts:
            return 0
        self._writes += 1
        async with scoped_session(self._maker, ctx) as session:
            got = await session.execute(
                text(
                    "INSERT INTO app.growbot_journal"
                    " (persona_id, robot, sid, t, row_hash, event, row, head)"
                    " SELECT CAST(:persona AS uuid), :robot, :sid, u.t, u.h, u.e,"
                    " CAST(u.r AS jsonb), CAST(:head AS jsonb)"
                    " FROM unnest(CAST(:ts AS bigint[]), CAST(:hashes AS text[]),"
                    " CAST(:events AS text[]), CAST(:bodies AS text[])) AS u(t, h, e, r)"
                    " ON CONFLICT (sid, t, row_hash) DO NOTHING RETURNING id"
                ),
                {
                    "persona": batch.persona_id or None,
                    "robot": batch.robot,
                    "sid": batch.sid,
                    "head": head,
                    "ts": ts,
                    "hashes": hashes,
                    "events": events,
                    "bodies": bodies,
                },
            )
            new = len(got.fetchall())
            if (self._writes - 1) % _PRUNE_EVERY == 0:
                await session.execute(
                    text(
                        "DELETE FROM app.growbot_journal"
                        " WHERE created_at < now() - make_interval(secs => :keep)"
                    ),
                    {"keep": KEEP.total_seconds()},
                )
            return new

    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 500
    ) -> list[DiaryRow]:
        """Rows the box received since `since`, oldest first, after row `after_id`."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, created_at, persona_id::text AS persona_id, robot, sid, t,"
                        " row, head FROM app.growbot_journal"
                        " WHERE created_at >= :since AND id > :after"
                        " ORDER BY id LIMIT :limit"
                    ),
                    {"since": since, "after": after_id, "limit": limit},
                )
            ).mappings()
            return [
                DiaryRow(
                    id=r["id"],
                    created_at=r["created_at"],
                    persona_id=r["persona_id"],
                    robot=r["robot"],
                    sid=r["sid"],
                    t=r["t"],
                    row=r["row"],
                    head=r["head"],
                )
                for r in rows
            ]
