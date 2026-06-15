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

import uuid
from dataclasses import dataclass

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
    `kind='agent'`. One call records a whole dispatched pipeline: the run row plus a
    step per enqueued job, committed together so a run never logs steps for jobs it
    did not produce (or vice versa)."""

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
        """Write one completed pipeline run + its enqueued-job steps; return the run
        id. The run is `done` on write: the dispatcher's job is to ENQUEUE the
        pipeline's steps (the executor runs them later under their own job records),
        so the dispatch itself completes the moment the jobs are on the queue."""
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
                    status="done",
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
