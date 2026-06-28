"""Persisting the agent run log: one `runs` row per turn-loop execution and a
`run_steps` row per step.

The `runs`/`run_steps` tables are shared with the workflow engine (migration 0037),
so every agent run is stamped `kind='agent'` — the DB CHECK then enforces that its
`session_id`/`prompt_version` are present. An agent turn runs under the owner's
scope, so `ran_as` stays the default `'scoped'` (the engine's system/cross-domain
runs are the ones that record `'system'`); this log writes agent behavior
identically to before the unification.

The loop takes a `RunRecorder` (loop.py) that only knows how to record a `step`.
`AgentRunLog` owns the run lifecycle (start/finish) and the SQL; `bound()` hands
the loop a recorder pinned to one run + context, so the loop stays database-free
and the caller owns the run's start and finish (P4.5 wires this into /chat).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import Run, RunStep
from jbrain.models.workflow import Trigger
from jbrain.queue import queued_depth


class AgentRunLog:
    """CRUD for the agent run log, on owner-scoped sessions (runs are owner-only)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def start(self, ctx: SessionContext, *, session_id: str, prompt_version: str) -> str:
        async with scoped_session(self._maker, ctx) as session:
            # kind='agent' is explicit so the shared run log's CHECK admits this row
            # (it requires session_id + prompt_version for agent runs).
            run = Run(
                kind="agent",
                session_id=uuid.UUID(session_id),
                prompt_version=prompt_version,
            )
            session.add(run)
            await session.flush()
            return str(run.id)

    async def step(
        self,
        ctx: SessionContext,
        run_id: str,
        *,
        idx: int,
        kind: str,
        name: str,
        ok: bool,
        cost_tokens: int,
        tool_version: int | None = None,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            session.add(
                RunStep(
                    run_id=uuid.UUID(run_id),
                    idx=idx,
                    kind=kind,
                    name=name,
                    tool_version=tool_version,
                    ok=ok,
                    cost_tokens=cost_tokens,
                )
            )

    async def finish(
        self,
        ctx: SessionContext,
        run_id: str,
        *,
        status: str,
        stop_reason: str,
        step_count: int,
        cost_tokens: int,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(Run)
                .where(Run.id == uuid.UUID(run_id))
                .values(
                    status=status,
                    stop_reason=stop_reason,
                    step_count=step_count,
                    cost_tokens=cost_tokens,
                    ended_at=datetime.now(UTC),
                )
            )

    def bound(self, ctx: SessionContext, run_id: str) -> "BoundRecorder":
        """A `RunRecorder` (loop.py) pinned to one run and context."""
        return BoundRecorder(self, ctx, run_id)


@dataclass(frozen=True)
class BoundRecorder:
    """Adapts AgentRunLog to the loop's RunRecorder protocol: forwards each
    `step` to the bound run + context."""

    log: AgentRunLog
    ctx: SessionContext
    run_id: str

    async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
        await self.log.step(
            self.ctx, self.run_id, idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens
        )


class StepTally:
    """Wraps a `RunRecorder` to total a turn's steps and cost as it records them.

    `run_stream` (loop.py) yields ChatEvents, not the step/cost tallies the run
    summary needs, so both turn drivers — the /chat endpoint and the headless task
    runner — count the steps as the loop records each one, then write the totals to
    the run row. Forwards every `step` unchanged to the inner recorder."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.steps = 0
        self.cost = 0

    async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
        self.steps += 1
        self.cost += cost_tokens
        await self._inner.step(  # type: ignore[attr-defined]
            idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens
        )


def _duration_ms(started_at: datetime, ended_at: datetime | None) -> int | None:
    """ms a run spent; None while it is still running (no honest end yet)."""
    if ended_at is None:
        return None
    return int((ended_at - started_at).total_seconds() * 1000)


@dataclass(frozen=True)
class RunSummary:
    """A row in the Ops run log: enough to render a list entry without loading
    its steps."""

    id: str
    kind: str
    status: str
    name: str
    started_at: datetime
    duration_ms: int | None
    step_count: int
    cost_tokens: int
    last_error: str | None
    # A live "processed X of Y" line while the run is in flight; null once it closes.
    progress_note: str | None


@dataclass(frozen=True)
class RunStepView:
    """A node in the split-panel step tree."""

    idx: int
    kind: str
    name: str
    ok: bool
    cost_tokens: int
    job_id: str | None
    error: str | None
    # The step's captured structured-log trace (engine steps; null for agent steps
    # and any job that logged nothing) — the Runs "full logs" review view.
    detail: list[dict[str, object]] | None


@dataclass(frozen=True)
class RunDetail:
    """A run plus its ordered step tree (the split-panel payload)."""

    id: str
    kind: str
    status: str
    name: str
    started_at: datetime
    duration_ms: int | None
    step_count: int
    cost_tokens: int
    stop_reason: str | None
    progress_note: str | None
    steps: list[RunStepView]


class RunLogReader:
    """Owner-scoped reads of the run log for the Ops "Runs" surface. Runs are
    owner-only (RLS), so every read flows through `scoped_session` under the
    owner's context — a non-owner session sees an empty log."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    @staticmethod
    def _display_name(kind: str, pipeline: str | None, trigger_pipeline: str | None) -> str:
        # The list label, per the mock: the pipeline (or its trigger's pipeline)
        # names the run; agent runs are session-less here so they read 'agent'.
        return pipeline or trigger_pipeline or kind or "agent"

    async def queue_depth(self, ctx: SessionContext) -> int:
        """The job-queue backlog for the Ops "Runs" queue-depth tile — jobs waiting
        (status='queued') in app.jobs. Reads under the owner context like the rest of
        this reader; the jobs table is owner-only RLS, so a non-owner sees zero."""
        return await queued_depth(self._maker, ctx)

    async def _queued_pipeline_ids(
        self, session: AsyncSession, candidates: list[uuid.UUID]
    ) -> set[str]:
        """Of these in-flight pipeline runs, the ids whose every enqueued step is
        still waiting (its job is status='queued') — so no step has started and the
        run is honestly QUEUED behind the single-threaded worker, not running.

        Derived, never stored: the `runs.status` CHECK (migration 0016) has no
        'queued', and the worker already serializes the jobs — this only surfaces
        that truth so the dashboard shows 1 running + N queued, not N running. A run
        counts as started (kept 'running') the moment any step's job is missing, aged
        out, or past 'queued', so we only ever demote when certain nothing ran."""
        if not candidates:
            return set()
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT r.id FROM app.runs r"
                        " WHERE r.id IN :ids"
                        "   AND EXISTS (SELECT 1 FROM app.run_steps s WHERE s.run_id = r.id)"
                        "   AND NOT EXISTS ("
                        "     SELECT 1 FROM app.run_steps s"
                        "     LEFT JOIN app.jobs j ON j.id = s.job_id"
                        "     WHERE s.run_id = r.id"
                        "       AND (s.job_id IS NULL OR j.id IS NULL OR j.status <> 'queued'))"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": candidates},
                )
            )
            .scalars()
            .all()
        )
        return {str(r) for r in rows}

    @staticmethod
    def _effective_status(run: Run, queued_ids: set[str]) -> str:
        """A run's display status: the stored value, except an in-flight pipeline run
        whose steps are all still queued reads as 'queued' (see `_queued_pipeline_ids`)."""
        return "queued" if str(run.id) in queued_ids else run.status

    async def list_recent(self, ctx: SessionContext, *, limit: int = 50) -> list[RunSummary]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(Run, Trigger.pipeline)
                    .outerjoin(Trigger, Run.trigger_id == Trigger.id)
                    .order_by(Run.started_at.desc())
                    .limit(limit)
                )
            ).all()
            # In-flight pipeline runs whose steps have not started yet read as
            # 'queued' (derived, not stored) so the dashboard shows them waiting.
            queued_ids = await self._queued_pipeline_ids(
                session,
                [run.id for run, _ in rows if run.kind == "pipeline" and run.status == "running"],
            )
            out: list[RunSummary] = []
            for run, trigger_pipeline in rows:
                last_error = None
                # The run log stores 'error' for a failed run (migration 0016
                # CHECK); the Ops surface renders that as "failed".
                if run.status == "error":
                    # Surface the first failing step's name as the list-row error
                    # hint; the full message lives in the detail step tree.
                    last_error = (
                        await session.execute(
                            select(RunStep.name)
                            .where(RunStep.run_id == run.id, RunStep.ok.is_(False))
                            .order_by(RunStep.idx)
                            .limit(1)
                        )
                    ).scalar()
                out.append(
                    RunSummary(
                        id=str(run.id),
                        kind=run.kind,
                        status=self._effective_status(run, queued_ids),
                        name=self._display_name(run.kind, run.pipeline, trigger_pipeline),
                        started_at=run.started_at,
                        duration_ms=_duration_ms(run.started_at, run.ended_at),
                        step_count=run.step_count,
                        cost_tokens=run.cost_tokens,
                        last_error=last_error,
                        progress_note=run.progress_note,
                    )
                )
            return out

    async def load(self, ctx: SessionContext, run_id: str) -> RunDetail | None:
        try:
            rid = uuid.UUID(run_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    select(Run, Trigger.pipeline)
                    .outerjoin(Trigger, Run.trigger_id == Trigger.id)
                    .where(Run.id == rid)
                )
            ).one_or_none()
            if row is None:
                return None
            run, trigger_pipeline = row
            queued_ids = (
                await self._queued_pipeline_ids(session, [run.id])
                if run.kind == "pipeline" and run.status == "running"
                else set()
            )
            steps = (
                (
                    await session.execute(
                        select(RunStep).where(RunStep.run_id == rid).order_by(RunStep.idx)
                    )
                )
                .scalars()
                .all()
            )
            return RunDetail(
                id=str(run.id),
                kind=run.kind,
                status=self._effective_status(run, queued_ids),
                name=self._display_name(run.kind, run.pipeline, trigger_pipeline),
                started_at=run.started_at,
                duration_ms=_duration_ms(run.started_at, run.ended_at),
                step_count=run.step_count,
                cost_tokens=run.cost_tokens,
                stop_reason=run.stop_reason,
                progress_note=run.progress_note,
                steps=[
                    RunStepView(
                        idx=s.idx,
                        kind=s.kind,
                        name=s.name,
                        ok=s.ok,
                        cost_tokens=s.cost_tokens,
                        job_id=str(s.job_id) if s.job_id is not None else None,
                        # A not-ok step is a failure; we carry its name as the
                        # error text (the step has no free-form message column).
                        error=None if s.ok else s.name,
                        detail=s.detail,
                    )
                    for s in steps
                ],
            )
