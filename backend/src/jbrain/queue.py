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
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

SYSTEM_CTX = SessionContext(principal_id="worker", principal_kind="owner")

BACKOFF_CAP = timedelta(hours=1)

# A 'running' job whose lock is older than this belongs to a dead worker:
# no handler runs anywhere near 10 minutes, so it is safe to reclaim.
STALE_LOCK = timedelta(minutes=10)


class PermanentJobError(Exception):
    """Raised by a handler when retrying cannot help (e.g. an extraction that
    stayed malformed through the adapter's re-ask) — the worker fails the job
    immediately instead of burning retries."""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    # The E1 scope carrier (migration 0039): the triggering principal + the
    # most-restrictive domain its trigger touched. BOTH NULL = a system job (every
    # job today, and the six shipped kinds) — the worker runs it under SYSTEM_CTX
    # unchanged. When both are present, the worker narrows the execution context to
    # this scope (no confused deputy, ASSISTANT.md I-8). A partial stamp is a
    # fail-closed error in db.session.narrowed_context — never a silent widening.
    principal_id: str | None = None
    domain_code: str | None = None

    @property
    def is_stamped(self) -> bool:
        """Whether this job carries a triggering scope (owner/agent-triggered) vs
        being a system job. A partial stamp still reports stamped so the worker
        routes it through the fail-closed narrowing rather than treating it as
        system — a half-stamp must never earn the all-domains scope."""
        return self.principal_id is not None or self.domain_code is not None


ACTIVE_STATUSES = ("queued", "running")


class JobEnqueuer(Protocol):
    """The slice of the queue the API needs (full claim/complete is worker-side)."""

    async def enqueue(
        self,
        ctx: SessionContext,
        kind: str,
        payload: dict[str, Any],
        *,
        principal_id: str | None = None,
        domain_code: str | None = None,
    ) -> str: ...

    async def has_active(
        self,
        ctx: SessionContext,
        kind: str,
        *,
        payload_field: str,
        value: str,
        statuses: tuple[str, ...] = ACTIVE_STATUSES,
    ) -> bool: ...

    async def has_active_ocr_for_note(self, ctx: SessionContext, note_id: str) -> bool: ...

    async def has_active_transcribe_for_note(self, ctx: SessionContext, note_id: str) -> bool: ...

    async def has_active_analysis(self, ctx: SessionContext, note_id: str) -> bool: ...


class PgJobQueue:
    """Bound-sessionmaker facade over the module functions, for DI in the app."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def enqueue(
        self,
        ctx: SessionContext,
        kind: str,
        payload: dict[str, Any],
        *,
        principal_id: str | None = None,
        domain_code: str | None = None,
    ) -> str:
        return await enqueue(
            self._maker, ctx, kind, payload, principal_id=principal_id, domain_code=domain_code
        )

    async def has_active(
        self,
        ctx: SessionContext,
        kind: str,
        *,
        payload_field: str,
        value: str,
        statuses: tuple[str, ...] = ACTIVE_STATUSES,
    ) -> bool:
        return await has_active(
            self._maker, ctx, kind, payload_field=payload_field, value=value, statuses=statuses
        )

    async def has_active_ocr_for_note(self, ctx: SessionContext, note_id: str) -> bool:
        return await has_active_ocr_for_note(self._maker, ctx, note_id)

    async def has_active_transcribe_for_note(self, ctx: SessionContext, note_id: str) -> bool:
        return await has_active_transcribe_for_note(self._maker, ctx, note_id)

    async def has_active_analysis(self, ctx: SessionContext, note_id: str) -> bool:
        return await has_active_analysis(self._maker, ctx, note_id)


def reclaim_attempts(attempts: int, max_attempts: int) -> tuple[int, bool]:
    """A stale-lock reclaim costs an attempt, so a worker-killing job still
    exhausts max_attempts instead of crash-looping forever."""
    attempts += 1
    return attempts, attempts >= max_attempts


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
    *,
    principal_id: str | None = None,
    domain_code: str | None = None,
) -> str:
    """Insert a queued job and return its id.

    `principal_id`/`domain_code` are the E1 scope stamp (migration 0039): pass BOTH
    to record the triggering principal + domain so the worker narrows the job's
    execution context to that scope (no confused deputy, I-8). Default NULL/NULL = a
    system job (every caller today), which the worker runs under SYSTEM_CTX exactly
    as before — the six shipped kinds are unchanged. The stamp is fail-closed at
    *use*: a partial stamp narrows to nothing and raises in the worker, never a
    silent widening (db.session.narrowed_context)."""
    job_id = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.jobs (id, kind, payload, principal_id, domain_code)"
                " VALUES (:id, :kind, cast(:payload AS jsonb), :principal_id, :domain_code)"
            ),
            {
                "id": job_id,
                "kind": kind,
                "payload": json.dumps(payload),
                "principal_id": principal_id,
                "domain_code": domain_code,
            },
        )
    return job_id


async def has_active(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    kind: str,
    *,
    payload_field: str,
    value: str,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> bool:
    """Whether a job of `kind` in one of `statuses` carries this payload value —
    the API's duplicate guard (e.g. 409 on a second on-demand analyze). The
    ingest gate narrows `statuses` to queued-only: a RUNNING analyze may have
    read stale chunks, so it never suppresses a fresh enqueue."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.jobs WHERE kind = :kind"
                    " AND status IN :statuses"
                    " AND payload->>:field = :value LIMIT 1"
                ).bindparams(bindparam("statuses", expanding=True)),
                {
                    "kind": kind,
                    "field": payload_field,
                    "value": value,
                    "statuses": list(statuses),
                },
            )
        ).first()
    return row is not None


async def has_active_kind(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    kind: str,
    *,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> bool:
    """Whether ANY job of `kind` is in one of `statuses` — a payload-keyless dedup
    for an idempotent sweep that carries no per-target key (e.g. consolidate_predicates).
    `has_active` cannot express this: it is structurally payload-keyed
    (`payload->>:field = :value`). Mirrors the kind-only guard
    `backfill_sync_predicates` already uses
    (`WHERE kind = :kind AND status IN ('queued','running')`), so a second sweep is
    suppressed while one is queued or running."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.jobs WHERE kind = :kind AND status IN :statuses LIMIT 1"
                ).bindparams(bindparam("statuses", expanding=True)),
                {"kind": kind, "statuses": list(statuses)},
            )
        ).first()
    return row is not None


async def queued_depth(maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> int:
    """How many jobs are waiting to run (status='queued') — the Ops "Runs"
    queue-depth tile. Running jobs are excluded: they have already started, so they
    are not waiting. A scheduled retry whose backoff has not elapsed is still
    counted (it is queued), matching the table's own notion of the backlog."""
    async with scoped_session(maker, ctx) as session:
        count = (
            await session.execute(text("SELECT count(*) FROM app.jobs WHERE status = 'queued'"))
        ).scalar()
    return int(count or 0)


async def has_active_analysis(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    note_id: str,
    *,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> bool:
    """Whether an integrate_note job is active for this note — the guard that
    keeps the trigger from enqueuing a second pass over a note already in
    flight (both would write the same note)."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.jobs WHERE kind = 'integrate_note'"
                    " AND status IN :statuses AND payload->>'note_id' = :nid LIMIT 1"
                ).bindparams(bindparam("statuses", expanding=True)),
                {"statuses": list(statuses), "nid": note_id},
            )
        ).first()
    return row is not None


async def has_active_ocr_for_note(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, note_id: str
) -> bool:
    """Whether any queued/running ocr_attachment job targets one of this
    note's attachments — the outstanding-vision-work signal the analysis gate
    keys on (jbrain.ingest.pipeline)."""
    return await _has_active_attachment_job(maker, ctx, "ocr_attachment", note_id)


async def has_active_transcribe_for_note(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, note_id: str
) -> bool:
    """The audio twin of has_active_ocr_for_note: whether any queued/running
    transcribe_attachment job targets one of this note's attachments — the other
    half of the outstanding-attachment-work signal the analysis gate keys on."""
    return await _has_active_attachment_job(maker, ctx, "transcribe_attachment", note_id)


async def _has_active_attachment_job(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, kind: str, note_id: str
) -> bool:
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.jobs j"
                    " WHERE j.kind = :kind"
                    " AND j.status IN ('queued', 'running')"
                    " AND j.payload->>'attachment_id' IN ("
                    "     SELECT a.id::text FROM app.attachments a WHERE a.note_id = :note_id"
                    " ) LIMIT 1"
                ),
                {"kind": kind, "note_id": note_id},
            )
        ).first()
    return row is not None


async def claim(maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> Job | None:
    """Atomically claim the next runnable job, or None when the queue is idle.

    Also reaps stale 'running' jobs (lock older than STALE_LOCK — the worker
    died mid-job): a reclaim counts as another attempt, and an exhausted
    reclaim fails the job permanently instead of re-running it.
    """
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, kind, payload::text AS payload, attempts, max_attempts,
                           principal_id, domain_code,
                           status = 'running' AS stale
                    FROM app.jobs
                    WHERE (status = 'queued' AND run_after <= now())
                       OR (status = 'running'
                           AND locked_at < now() - make_interval(secs => :stale_secs))
                    ORDER BY run_after
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                ),
                {"stale_secs": STALE_LOCK.total_seconds()},
            )
        ).first()
        if row is None:
            return None
        attempts = row.attempts
        if row.stale:
            attempts, exhausted = reclaim_attempts(attempts, row.max_attempts)
            if exhausted:
                await session.execute(
                    text(
                        "UPDATE app.jobs SET status = 'failed', attempts = :attempts,"
                        " last_error = 'stale lock reclaimed; attempts exhausted',"
                        " locked_at = NULL, finished_at = now() WHERE id = :id"
                    ),
                    {"id": str(row.id), "attempts": attempts},
                )
                return None
        await session.execute(
            text(
                "UPDATE app.jobs SET status = 'running', locked_at = now(),"
                " attempts = :attempts WHERE id = :id"
            ),
            {"id": str(row.id), "attempts": attempts},
        )
        return Job(
            id=str(row.id),
            kind=row.kind,
            payload=json.loads(row.payload),
            attempts=attempts,
            max_attempts=row.max_attempts,
            # The E1 scope stamp travels with the claimed job; the worker decides
            # narrowed vs SYSTEM_CTX from it. uuid columns come back as UUID, so
            # stringify for the GUC (None stays None — an unstamped/system job).
            principal_id=str(row.principal_id) if row.principal_id is not None else None,
            domain_code=row.domain_code,
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
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    job_id: str,
    error: str,
    *,
    permanent: bool = False,
) -> bool:
    """Record a failure: requeue with exponential backoff, or fail permanently.

    `permanent` short-circuits the retry budget for failures retrying cannot
    fix (see PermanentJobError). Returns whether the job is now exhausted
    (status='failed') so the worker can run kind-specific fallbacks — e.g.
    body-only analysis after OCR gives up.
    """
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text("SELECT attempts, max_attempts FROM app.jobs WHERE id = :id FOR UPDATE"),
                {"id": job_id},
            )
        ).first()
        if row is None:
            return False
        attempts = row.attempts + 1
        exhausted = permanent or attempts >= row.max_attempts
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
    return exhausted


async def defer(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    job_id: str,
    delay: timedelta,
    *,
    reason: str,
) -> None:
    """Reschedule a claimed (running) job to run again after `delay`, WITHOUT burning
    an attempt — the worker's "not now, try again soon" for an unmet precondition.

    Distinct from `fail`: a precondition that isn't satisfied yet (e.g. a local model
    not loaded) is not an error, so attempts/max_attempts are untouched and the job
    never reaches status='failed' however long it waits. The job returns to 'queued'
    with a future `run_after`, so the claim loop skips it until the delay elapses, then
    re-evaluates the gate. `reason` is recorded in `last_error` (the only diagnostic
    column) prefixed 'deferred:' so Ops can see why it is waiting without mistaking it
    for a failure — status stays 'queued'."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                """
                UPDATE app.jobs
                SET status = 'queued',
                    locked_at = NULL,
                    last_error = :reason,
                    run_after = now() + make_interval(secs => :delay)
                WHERE id = :id AND status = 'running'
                """
            ),
            {"id": job_id, "reason": f"deferred: {reason}", "delay": delay.total_seconds()},
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
        # session.execute is typed as Result, but INSERT always yields a
        # CursorResult carrying rowcount.
        return cast(CursorResult[Any], result).rowcount or 0


INTEGRATION_BACKFILL_LIMIT = 100

# Owner-ahead ordering hook (N14). The backfill drains oldest-first, but the leading
# rank term keeps trusted owner notes ahead of untrusted-origin ones. Now LIVE (Phase 7,
# guided intake): `n.provenance = 'untrusted_origin'` evaluates false (sorts first) for
# every owner/agent note and true (sorts last) for a stranger-authored intake note, so a
# flood of approved intake notes can never starve the owner's own notes of integration.
# It is an EXPRESSION, not a bare constant: Postgres reads a bare integer in ORDER BY as
# an (invalid) ordinal position; only an expression sorts all matching rows together.
INTEGRATION_BACKFILL_ORDER_BY = "(n.provenance = 'untrusted_origin'), n.created_at"


async def backfill_pending_integration(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    limit: int = INTEGRATION_BACKFILL_LIMIT,
) -> int:
    """Enqueue integrate_note for indexed notes not yet integrated — the v3
    cutover backfill (W3.3). BOUNDED per call: migration 0029 defaulted EVERY
    existing note to 'pending_integration', so an unbounded sweep would push the
    whole corpus through the costlier Integrator at once. Oldest-first
    (created_at); each integrated note drops out of `integration_state <>
    'integrated'`, so repeated boots drain the backlog within budget. Skips a note
    with an active integrate_note job or outstanding OCR. Ordered by
    INTEGRATION_BACKFILL_ORDER_BY — the owner-ahead (N14) seam, inert today (see
    that constant)."""
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(
            text(
                # INTEGRATION_BACKFILL_ORDER_BY is a module constant, never
                # caller input — interpolation is safe.
                f"""
                INSERT INTO app.jobs (id, kind, payload)
                SELECT gen_random_uuid(), 'integrate_note',
                       jsonb_build_object('note_id', n.id)
                FROM app.notes n
                WHERE n.ingest_state = 'indexed'
                  AND n.deleted_at IS NULL
                  AND n.integration_state <> 'integrated'
                  AND NOT EXISTS (
                      SELECT 1 FROM app.jobs j
                      WHERE j.kind = 'integrate_note'
                        AND j.status IN ('queued', 'running')
                        AND j.payload ->> 'note_id' = n.id::text
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app.jobs j
                      JOIN app.attachments att
                        ON att.id::text = j.payload ->> 'attachment_id'
                      WHERE j.kind = 'ocr_attachment'
                        AND j.status IN ('queued', 'running')
                        AND att.note_id = n.id
                  )
                ORDER BY {INTEGRATION_BACKFILL_ORDER_BY}
                LIMIT :lim
                """
            ),
            {"lim": limit},
        )
        return cast(CursorResult[Any], result).rowcount or 0


async def backfill_consolidate(maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> int:
    """Enqueue ONE consolidate_predicates sweep when none is pending — the boot
    self-heal that normalizes drift predicates left by an older prompt version
    onto their canonical address. Recurring + on-demand scheduling is deferred
    to the Phase-5 workflow engine (docs/ROADMAP.md "Scheduled-task migration")."""
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO app.jobs (id, kind, payload)
                SELECT gen_random_uuid(), 'consolidate_predicates', '{}'::jsonb
                WHERE NOT EXISTS (
                    SELECT 1 FROM app.jobs
                    WHERE kind = 'consolidate_predicates'
                      AND status IN ('queued', 'running')
                )
                """
            )
        )
        return cast(CursorResult[Any], result).rowcount or 0


async def backfill_sync_predicates(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> int:
    """Enqueue ONE sync_predicates job at boot when none is pending — the
    self-heal that keeps the canonical_predicates index in step with the schema
    registry (predicate canonicalization Phase 2). The job is idempotent (upsert
    DO NOTHING + embed only missing/stale rows), so scheduling is unconditional
    beyond the no-duplicate guard, mirroring backfill_consolidate. This is what
    seeds the empty table on first boot."""
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO app.jobs (id, kind, payload)
                SELECT gen_random_uuid(), 'sync_predicates', '{}'::jsonb
                WHERE NOT EXISTS (
                    SELECT 1 FROM app.jobs
                    WHERE kind = 'sync_predicates' AND status IN ('queued', 'running')
                )
                """
            )
        )
        return cast(CursorResult[Any], result).rowcount or 0


async def backfill_unembedded_notes(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> int:
    """Enqueue embed_note for notes with NULL-embedding chunks and no live job.

    Self-heals after an embedding-model change wipe or a Step-2-era index
    (chunks existed before the embed pipeline did).
    """
    async with scoped_session(maker, ctx) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO app.jobs (id, kind, payload)
                SELECT gen_random_uuid(), 'embed_note',
                       jsonb_build_object('note_id', n.id)
                FROM app.notes n
                WHERE n.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM app.chunks c
                      WHERE c.note_id = n.id AND c.embedding IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM app.jobs j
                      WHERE j.kind = 'embed_note'
                        AND j.status IN ('queued', 'running')
                        AND j.payload ->> 'note_id' = n.id::text
                  )
                """
            )
        )
        return cast(CursorResult[Any], result).rowcount or 0
