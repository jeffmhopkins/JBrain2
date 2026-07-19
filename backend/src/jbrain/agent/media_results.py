"""Run-scoped results for deferred media-analysis tool calls (DEFERRED_TOOL_CALLS_PLAN.md P2).

The storage behind a `deferred` `analyze_stream`: one `app.media_analysis_results` row
per off-turn analysis, holding its live progress (the `task_status` card polls it), its
final result (in the `video_analysis` view's data shape, so the card swaps straight to
the existing component), and its status. A URL has no attachment to cache against
(unlike the `analyze_video` path, which keys on the blob sha256), so this row is where a
deferred URL analysis lives.

Managed with raw SQL over an RLS-scoped session, exactly like `jbrain.queue` /
`jbrain.workflow.runlog` (the row is owner-only, migration 0132). The kicking chat turn
and the poll endpoint use the owner session; the worker writes progress/result under its
own `SYSTEM_CTX` (an owner-kind context). Writes that advance the work are guarded on
`status='running'`, so a `cancel` mid-flight is sticky — a later `complete`/`fail`/
progress from the (now cancelled) job is a no-op and never resurrects the row.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


@dataclass(frozen=True)
class MediaResult:
    """One deferred analysis: its live progress + final result + status, as the poll
    endpoint and the completion (auto-resume) path read them."""

    id: str
    session_id: str
    status: str  # running | done | failed | canceled
    progress: dict[str, Any]  # {step, total, label}
    result: dict[str, Any] | None  # the video_analysis view data, once done
    error: str | None
    job_id: str | None
    created_at: str  # ISO — the card bases its elapsed timer on this, so a reopen is seamless
    resumed_at: str | None  # ISO — when the one auto-resume turn was claimed (None until claimed)


def _row_to_result(row: Any) -> MediaResult:
    return MediaResult(
        id=str(row.id),
        session_id=row.session_id,
        status=row.status,
        progress=dict(row.progress or {}),
        result=dict(row.result) if row.result is not None else None,
        error=row.error,
        job_id=str(row.job_id) if row.job_id is not None else None,
        created_at=row.created_at.isoformat() if row.created_at is not None else "",
        resumed_at=row.resumed_at.isoformat() if row.resumed_at is not None else None,
    )


async def create(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    session_id: str,
    run_id: str | None = None,
) -> str:
    """Open a running result row for a chat session and return its id (the poll handle
    the `task_status` card carries). The job id is attached once the job is enqueued."""
    result_id = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.media_analysis_results (id, session_id, run_id)"
                " VALUES (:id, :session_id, :run_id)"
            ),
            {"id": result_id, "session_id": session_id, "run_id": run_id},
        )
    return result_id


async def attach_job(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, result_id: str, job_id: str
) -> None:
    """Record the queue job doing the work, so a Stop can cancel it."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("UPDATE app.media_analysis_results SET job_id = :job_id WHERE id = :id"),
            {"id": result_id, "job_id": job_id},
        )


async def set_progress(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    result_id: str,
    *,
    step: int,
    total: int,
    label: str,
) -> None:
    """Advance the card's live progress. A no-op once the row leaves 'running' (a
    cancelled/finished analysis is not re-animated by a late tick)."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.media_analysis_results"
                " SET progress = cast(:progress AS jsonb), updated_at = now()"
                " WHERE id = :id AND status = 'running'"
            ),
            {
                "id": result_id,
                "progress": json.dumps({"step": step, "total": total, "label": label}),
            },
        )


async def complete(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    result_id: str,
    *,
    result: dict[str, Any],
) -> None:
    """Mark the analysis done with its final `video_analysis`-shaped result. Guarded on
    'running' so it never overwrites a cancel."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.media_analysis_results"
                " SET status = 'done', result = cast(:result AS jsonb), updated_at = now()"
                " WHERE id = :id AND status = 'running'"
            ),
            {"id": result_id, "result": json.dumps(result)},
        )


async def fail(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    result_id: str,
    *,
    error: str,
) -> None:
    """Mark the analysis failed with a short reason. Guarded on 'running' so a cancel wins."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.media_analysis_results"
                " SET status = 'failed', error = :error, updated_at = now()"
                " WHERE id = :id AND status = 'running'"
            ),
            {"id": result_id, "error": error[:500]},
        )


async def cancel(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, result_id: str
) -> bool:
    """Request cancellation (the card's Stop): flip a still-running row to 'canceled' so
    the worker's watcher terminates the job and no late write resurrects it. Returns True
    if a running row was cancelled, False if it had already finished."""
    async with scoped_session(maker, ctx) as session:
        res = await session.execute(
            text(
                "UPDATE app.media_analysis_results SET status = 'canceled', updated_at = now()"
                " WHERE id = :id AND status = 'running'"
            ),
            {"id": result_id},
        )
        return cast(CursorResult[Any], res).rowcount > 0


async def claim_resume(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, result_id: str
) -> bool:
    """Claim a finished analysis' one auto-resume turn, atomically. Sets `resumed_at` iff
    the row is `done` and not yet claimed, and returns whether THIS caller won the claim.
    The `resumed_at IS NULL` guard makes it exactly-once across reloads, extra tabs, and a
    card that mounts already-done — the fix for a job that finished while nothing was
    watching the card, so the resume (with the transcript) never reached the model."""
    async with scoped_session(maker, ctx) as session:
        res = await session.execute(
            text(
                "UPDATE app.media_analysis_results SET resumed_at = now()"
                " WHERE id = :id AND status = 'done' AND resumed_at IS NULL"
            ),
            {"id": result_id},
        )
        return cast(CursorResult[Any], res).rowcount > 0


async def pending_resumes(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, session_id: str
) -> list[MediaResult]:
    """Finished analyses in a chat session whose one auto-resume turn was never claimed
    (`done` and `resumed_at IS NULL`), oldest first. The server-side backstop reads these
    to feed a finished-off-screen transcript into the next turn even when no `task_status`
    card was ever mounted to claim it — a headless Task run, or a chat never reopened."""
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, session_id, status, progress, result, error, job_id,"
                    " created_at, resumed_at"
                    " FROM app.media_analysis_results"
                    " WHERE session_id = :sid AND status = 'done' AND resumed_at IS NULL"
                    " ORDER BY created_at"
                ),
                {"sid": session_id},
            )
        ).all()
    return [_row_to_result(r) for r in rows]


async def get(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, result_id: str
) -> MediaResult | None:
    """Read one result row (the card's poll), or None if it is gone / out of scope."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, session_id, status, progress, result, error, job_id,"
                    " created_at, resumed_at"
                    " FROM app.media_analysis_results WHERE id = :id"
                ),
                {"id": result_id},
            )
        ).one_or_none()
    return _row_to_result(row) if row is not None else None


class MediaResults:
    """Bound-sessionmaker facade over the module functions, for DI in the app (the poll
    endpoint and the analyze_stream handler)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def create(
        self, ctx: SessionContext, *, session_id: str, run_id: str | None = None
    ) -> str:
        return await create(self._maker, ctx, session_id=session_id, run_id=run_id)

    async def attach_job(self, ctx: SessionContext, result_id: str, job_id: str) -> None:
        await attach_job(self._maker, ctx, result_id, job_id)

    async def cancel(self, ctx: SessionContext, result_id: str) -> bool:
        return await cancel(self._maker, ctx, result_id)

    async def claim_resume(self, ctx: SessionContext, result_id: str) -> bool:
        return await claim_resume(self._maker, ctx, result_id)

    async def pending_resumes(self, ctx: SessionContext, session_id: str) -> list[MediaResult]:
        return await pending_resumes(self._maker, ctx, session_id)

    async def get(self, ctx: SessionContext, result_id: str) -> MediaResult | None:
        return await get(self._maker, ctx, result_id)
