"""Postgres-backed job queue.

Jobs live in app.jobs, claimed with SELECT ... FOR UPDATE SKIP LOCKED so
concurrent workers never double-claim. Payloads carry row IDs only — never
note content — which is why a single owner-only RLS policy covers the table.

The worker runs with SYSTEM_CTX, an owner-kind session context: this is a
single-owner system and the worker is the owner's own machinery, so it
legitimately crosses every domain firewall (the jobs policy and the notes /
chunks policies all pass for `app.is_owner()`).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

SYSTEM_CTX = SessionContext(principal_id="worker", principal_kind="owner")

BACKOFF_CAP = timedelta(hours=1)


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


class JobEnqueuer(Protocol):
    """The slice of the queue the API needs (full claim/complete is worker-side)."""

    async def enqueue(self, ctx: SessionContext, kind: str, payload: dict[str, Any]) -> str: ...


class PgJobQueue:
    """Bound-sessionmaker facade over the module functions, for DI in the app."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def enqueue(self, ctx: SessionContext, kind: str, payload: dict[str, Any]) -> str:
        return await enqueue(self._maker, ctx, kind, payload)


def backoff(attempts: int) -> timedelta:
    """Retry delay after the Nth failed attempt: 2^N minutes, capped."""
    if attempts < 1:
        return timedelta(0)
    # min() on the exponent first so huge attempt counts can't overflow.
    return min(timedelta(minutes=2 ** min(attempts, 10)), BACKOFF_CAP)


async def enqueue(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    kind: str,
    payload: dict[str, Any],
) -> str:
    """Insert a queued job and return its id."""
    job_id = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload)"
                " VALUES (:id, :kind, cast(:payload AS jsonb))"
            ),
            {"id": job_id, "kind": kind, "payload": json.dumps(payload)},
        )
    return job_id


async def claim(maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> Job | None:
    """Atomically claim the next runnable job, or None when the queue is idle."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, kind, payload::text AS payload, attempts, max_attempts
                    FROM app.jobs
                    WHERE status = 'queued' AND run_after <= now()
                    ORDER BY run_after
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                )
            )
        ).first()
        if row is None:
            return None
        await session.execute(
            text("UPDATE app.jobs SET status = 'running', locked_at = now() WHERE id = :id"),
            {"id": str(row.id)},
        )
        return Job(
            id=str(row.id),
            kind=row.kind,
            payload=json.loads(row.payload),
            attempts=row.attempts,
            max_attempts=row.max_attempts,
        )


async def complete(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, job_id: str
) -> None:
    """Mark a running job done; a repeat call is a harmless no-op."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.jobs SET status = 'done', finished_at = now()"
                " WHERE id = :id AND status = 'running'"
            ),
            {"id": job_id},
        )


async def fail(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, job_id: str, error: str
) -> None:
    """Record a failure: requeue with exponential backoff, or fail permanently."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text("SELECT attempts, max_attempts FROM app.jobs WHERE id = :id FOR UPDATE"),
                {"id": job_id},
            )
        ).first()
        if row is None:
            return
        attempts = row.attempts + 1
        exhausted = attempts >= row.max_attempts
        await session.execute(
            text(
                """
                UPDATE app.jobs
                SET attempts = :attempts,
                    last_error = :error,
                    locked_at = NULL,
                    status = :status,
                    run_after = now() + make_interval(secs => :delay),
                    finished_at = CASE WHEN :status = 'failed' THEN now() ELSE finished_at END
                WHERE id = :id
                """
            ),
            {
                "id": job_id,
                "attempts": attempts,
                "error": error,
                "status": "failed" if exhausted else "queued",
                "delay": backoff(attempts).total_seconds(),
            },
        )


async def backfill_pending_notes(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> int:
    """Enqueue ingest_note for every un-ingested note lacking an active job.

    ingest_state='pending' is the backfill marker: migration 0003 stamps all
    pre-existing notes with it, so startup automatically picks them up.
    """
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO app.jobs (id, kind, payload)
                SELECT gen_random_uuid(), 'ingest_note',
                       jsonb_build_object('note_id', n.id)
                FROM app.notes n
                WHERE n.ingest_state = 'pending'
                  AND n.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM app.jobs j
                      WHERE j.kind = 'ingest_note'
                        AND j.status IN ('queued', 'running')
                        AND j.payload ->> 'note_id' = n.id::text
                  )
                """
            )
        )
        return result.rowcount or 0
