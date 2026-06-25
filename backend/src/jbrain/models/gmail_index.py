"""Gmail metadata index ORM + repos (migration 0096).

`gmail_message_meta` holds one row per message — `From`/`Date`/`labels` only, never the
body — so the archivist can run exact, full-history sender analytics that Gmail's API
can't (no server-side group-by). `gmail_index_state` is the single per-principal control
+ progress row the resumable backfill checkpoints against. Both are owner-only; the repos
take the caller's already-RLS-scoped `AsyncSession`, so the firewall is Postgres', not
these methods'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import CursorResult, DateTime, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class GmailMessageMeta(Base):
    __tablename__ = "gmail_message_meta"
    __table_args__ = {"schema": "app"}

    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    gmail_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thread_id: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(Text, default="pending")
    sender_email: Mapped[str] = mapped_column(Text, default="")
    sender_domain: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GmailIndexState(Base):
    __tablename__ = "gmail_index_state"
    __table_args__ = {"schema": "app"}

    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    phase: Mapped[str] = mapped_column(Text, default="idle")
    enabled: Mapped[bool] = mapped_column(default=False)
    total_estimate: Mapped[int] = mapped_column(Integer, default=0)
    discovery_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_history_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class IndexProgress:
    """A snapshot for the Settings progress bar / the agent's status tool."""

    phase: str
    enabled: bool
    total_estimate: int
    indexed: int  # rows in state='done'
    pending: int
    last_history_id: str | None
    error: str | None


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def epoch_ms_to_dt(ms: int) -> datetime | None:
    """A Gmail internalDate (epoch ms) as an aware UTC datetime, or None when absent."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None


class GmailMetaRepo:
    """Reads/writes the metadata rows on a caller-supplied RLS-scoped session."""

    async def upsert_discovered(
        self, session: AsyncSession, principal_id: str, ids: list[tuple[str, str]]
    ) -> int:
        """Insert newly discovered (gmail_id, thread_id) as `pending` rows. ON CONFLICT DO
        NOTHING — a re-discovered id keeps its existing row (and its `done` metadata), so
        re-running discovery never clobbers fetched data or resets progress."""
        if not ids:
            return 0
        rows = [
            {"principal_id": principal_id, "gmail_id": gid, "thread_id": tid} for gid, tid in ids
        ]
        stmt = (
            pg_insert(GmailMessageMeta)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=[GmailMessageMeta.principal_id, GmailMessageMeta.gmail_id]
            )
        )
        result = cast("CursorResult[object]", await session.execute(stmt))
        return result.rowcount or 0

    async def clear(self, session: AsyncSession, principal_id: str) -> None:
        """Drop this principal's whole index — a true rebuild from scratch."""
        await session.execute(
            text("DELETE FROM app.gmail_message_meta WHERE principal_id = :pid"),
            {"pid": principal_id},
        )

    async def claim_pending(
        self, session: AsyncSession, principal_id: str, *, limit: int
    ) -> list[str]:
        """The next batch of not-yet-fetched gmail ids for this principal."""
        result = await session.execute(
            text(
                "SELECT gmail_id FROM app.gmail_message_meta"
                " WHERE principal_id = :pid AND state = 'pending' LIMIT :lim"
            ),
            {"pid": principal_id, "lim": limit},
        )
        return [row[0] for row in result]

    async def save_meta(
        self,
        session: AsyncSession,
        principal_id: str,
        gmail_id: str,
        *,
        sender_email: str,
        subject: str,
        sent_at: datetime | None,
        label_ids: list[str],
    ) -> None:
        await session.execute(
            text(
                "UPDATE app.gmail_message_meta SET state = 'done', sender_email = :se,"
                " sender_domain = :sd, subject = :subj, sent_at = :sent, label_ids = :labels,"
                " error = NULL, updated_at = now()"
                " WHERE principal_id = :pid AND gmail_id = :gid"
            ),
            {
                "pid": principal_id,
                "gid": gmail_id,
                "se": sender_email,
                "sd": _domain(sender_email),
                "subj": subject,
                "sent": sent_at,
                "labels": label_ids,
            },
        )

    async def mark_error(
        self, session: AsyncSession, principal_id: str, gmail_id: str, error: str
    ) -> None:
        await session.execute(
            text(
                "UPDATE app.gmail_message_meta SET state = 'error', error = :err,"
                " updated_at = now() WHERE principal_id = :pid AND gmail_id = :gid"
            ),
            {"pid": principal_id, "gid": gmail_id, "err": error[:500]},
        )

    async def counts(self, session: AsyncSession, principal_id: str) -> tuple[int, int]:
        """(indexed, pending) for this principal — the live progress numerator."""
        result = await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE state = 'done'),"
                " count(*) FILTER (WHERE state = 'pending')"
                " FROM app.gmail_message_meta WHERE principal_id = :pid"
            ),
            {"pid": principal_id},
        )
        row = result.one()
        return int(row[0]), int(row[1])

    async def top_senders(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        by: str = "domain",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[tuple[str, int]]:
        """Exact top senders by volume, grouped by domain (default) or full address, over
        an optional [since, until) window — the index's answer to the question sampling
        could only approximate."""
        # `col` is one of two literals chosen here, never caller input — safe to inline.
        col = "sender_domain" if by == "domain" else "sender_email"
        result = await session.execute(
            text(
                f"SELECT {col} AS k, count(*) AS n FROM app.gmail_message_meta"
                f" WHERE principal_id = :pid AND state = 'done' AND {col} <> ''"
                " AND (CAST(:since AS timestamptz) IS NULL OR sent_at >= :since)"
                " AND (CAST(:until AS timestamptz) IS NULL OR sent_at < :until)"
                f" GROUP BY {col} ORDER BY n DESC, k LIMIT :lim"
            ),
            {"pid": principal_id, "since": since, "until": until, "lim": limit},
        )
        return [(str(r[0]), int(r[1])) for r in result]

    async def volume_by_day(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[tuple[datetime, int]]:
        """Message count per UTC day over an optional [since, until) window — the per-day
        histogram (date bucketing) sampling can't produce."""
        result = await session.execute(
            text(
                "SELECT date_trunc('day', sent_at) AS d, count(*) AS n"
                " FROM app.gmail_message_meta"
                " WHERE principal_id = :pid AND state = 'done' AND sent_at IS NOT NULL"
                " AND (CAST(:since AS timestamptz) IS NULL OR sent_at >= :since)"
                " AND (CAST(:until AS timestamptz) IS NULL OR sent_at < :until)"
                " GROUP BY d ORDER BY d"
            ),
            {"pid": principal_id, "since": since, "until": until},
        )
        return [(r[0], int(r[1])) for r in result]


class GmailIndexStateRepo:
    """Reads/writes the single control+progress row, paired with the meta counts."""

    async def get(self, session: AsyncSession, principal_id: str) -> GmailIndexState | None:
        return await session.get(GmailIndexState, principal_id)

    async def upsert(self, session: AsyncSession, principal_id: str, **fields: object) -> None:
        values = {"principal_id": principal_id, "updated_at": func.now(), **fields}
        stmt = (
            pg_insert(GmailIndexState)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[GmailIndexState.principal_id],
                set_={k: v for k, v in values.items() if k != "principal_id"},
            )
        )
        await session.execute(stmt)

    async def progress(
        self, session: AsyncSession, principal_id: str, meta: GmailMetaRepo
    ) -> IndexProgress:
        state = await self.get(session, principal_id)
        indexed, pending = await meta.counts(session, principal_id)
        return IndexProgress(
            phase=state.phase if state else "idle",
            enabled=state.enabled if state else False,
            total_estimate=state.total_estimate if state else 0,
            indexed=indexed,
            pending=pending,
            last_history_id=state.last_history_id if state else None,
            error=state.error if state else None,
        )
