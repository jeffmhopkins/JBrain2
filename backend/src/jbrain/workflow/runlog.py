"""Persisting an engine pipeline run: one `runs` row (`kind='pipeline'`) per
dispatched event + a `run_steps` row per enqueued job.

This is the §8 "diagnosable from the run log alone" fill: when the dispatcher
LIVE-enqueues a pipeline (workflow/dispatcher.py), it records the dispatch here so
the engine path is auditable from `app.runs`, exactly like the agent loop is via
`agent/runlog.py` — the shared `runs`/`run_steps` tables (migration 0037). The
agent writer stamps `kind='agent'`; this writer stamps `kind='pipeline'`, the
discriminator that admits a session-less engine run under the table CHECK.

`ran_as` records E1's scope choice on the audit (docs/WORKFLOW_ENGINE_PLAN.md E1):
an event carrying a triggering principal + domain ran `scoped` (the dispatcher
narrowed to that stamp); a system/unstamped event ran `system`. Each `run_step`
carries the `job_id` it enqueued (a nullable FK, SET NULL on job age-out, N2) so a
run drills straight down to the executor jobs it produced.

Writes flow through `scoped_session` under the owner context (runs are owner-only
RLS), mirroring AgentRunLog — the dispatcher's claim loop already runs under
`SYSTEM_CTX` (the owner/system context), so the run row is owner-visible.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import Run, RunStep


@dataclass(frozen=True)
class EnqueuedStep:
    """One enqueued pipeline step to record as a `run_step`: the action's handler
    `kind` (the run-step name) and the `job_id` the dispatcher enqueued for it."""

    kind: str
    job_id: str


class PipelineRunLog:
    """Writer for engine pipeline runs on the unified `runs`/`run_steps` tables.

    Mirrors `agent/runlog.py`'s AgentRunLog style (owner-scoped sessions, the ORM
    models), but stamps `kind='pipeline'` for a session-less engine run rather than
    `kind='agent'`. One `record` call opens a RUNNING run the moment the pipeline's
    steps are enqueued; the worker calls `finalize_job_step` as each step's job
    finishes, so the run reflects real execution — status, duration, and tokens —
    rather than a 0-token placeholder that was `done` before any work ran."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def record(
        self,
        ctx: SessionContext,
        *,
        pipeline: str,
        trigger_id: str | None,
        ran_as: str,
        domain_code: str | None,
        principal_id: str | None,
        steps: list[EnqueuedStep],
    ) -> str:
        """Open a RUNNING pipeline run + a step per enqueued job; return the run id.
        The run is NOT done on write — its bound jobs run later in the worker, which
        finalizes each step (and, once every step's job is terminal, the run itself)
        via `finalize_job_step`. Steps start `ok=True` provisionally (the column is
        non-null); a job that fails flips its step to `ok=False` on finalize."""
        run_id = str(uuid.uuid4())
        async with scoped_session(self._maker, ctx) as session:
            session.add(
                Run(
                    id=uuid.UUID(run_id),
                    kind="pipeline",
                    pipeline=pipeline,
                    trigger_id=uuid.UUID(trigger_id) if trigger_id is not None else None,
                    ran_as=ran_as,
                    domain_code=domain_code,
                    principal_id=uuid.UUID(principal_id) if principal_id is not None else None,
                    status="running",
                    step_count=len(steps),
                )
            )
            for idx, step in enumerate(steps):
                session.add(
                    RunStep(
                        run_id=uuid.UUID(run_id),
                        idx=idx,
                        kind="action",
                        name=step.kind,
                        job_id=uuid.UUID(step.job_id),
                        ok=True,
                    )
                )
        return run_id


async def set_run_progress(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    job_id: str,
    note: str,
) -> None:
    """Update the live progress note on the RUNNING run that owns this job's step
    ("processed 15 of 30 emails"). The Ops "Runs" screen polls it while the run is in
    flight. A no-op when the job has no run step (an ad-hoc enqueue) or its run is no
    longer running. Best-effort: a progress write must never fail the executor job."""
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.runs r SET progress_note = :note"
                " FROM app.run_steps s"
                " WHERE s.job_id = :jid AND r.id = s.run_id AND r.status = 'running'"
            ),
            {"note": note, "jid": job_id},
        )


async def finalize_job_step(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    job_id: str,
    *,
    ok: bool,
    cost_tokens: int,
    detail: list[dict[str, object]] | None = None,
) -> None:
    """Stamp a job's terminal outcome + token cost (+ its captured log `detail`, the
    "full logs" review trace) onto its run-log step, then close the parent run once
    ALL its steps' jobs are terminal — setting status (error if any step failed, else
    done), `ended_at` (so the run shows a real duration), and the run's total tokens.
    A no-op when the job has no run step (an ad-hoc enqueue).

    The worker calls this AFTER the job's queue transition (so this job already reads
    as terminal), best-effort: a run-log hiccup must never fail the executor job."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.run_steps SET ok = :ok, cost_tokens = :tok,"
                    " detail = cast(:detail AS jsonb) WHERE job_id = :jid RETURNING run_id"
                ),
                {
                    "ok": ok,
                    "tok": cost_tokens,
                    "detail": json.dumps(detail) if detail else None,
                    "jid": job_id,
                },
            )
        ).first()
        if row is None:
            return  # this job isn't part of a dispatched pipeline run
        await session.execute(
            text(
                "UPDATE app.runs r SET"
                "   status = CASE WHEN EXISTS (SELECT 1 FROM app.run_steps s"
                "                              WHERE s.run_id = r.id AND NOT s.ok)"
                "                  THEN 'error' ELSE 'done' END,"
                "   ended_at = now(),"
                "   progress_note = NULL,"  # the run is closing; a live note no longer applies
                "   cost_tokens = (SELECT COALESCE(SUM(s.cost_tokens), 0)"
                "                  FROM app.run_steps s WHERE s.run_id = r.id)"
                " WHERE r.id = :rid AND r.status = 'running'"
                "   AND NOT EXISTS (SELECT 1 FROM app.run_steps s"
                "                   JOIN app.jobs j ON j.id = s.job_id"
                "                   WHERE s.run_id = :rid AND j.status IN ('queued', 'running'))"
            ),
            {"rid": str(row.run_id)},
        )
