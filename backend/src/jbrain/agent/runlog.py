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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import Run, RunStep
from jbrain.models.workflow import Trigger


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
                        status=run.status,
                        name=self._display_name(run.kind, run.pipeline, trigger_pipeline),
                        started_at=run.started_at,
                        duration_ms=_duration_ms(run.started_at, run.ended_at),
                        step_count=run.step_count,
                        cost_tokens=run.cost_tokens,
                        last_error=last_error,
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
                status=run.status,
                name=self._display_name(run.kind, run.pipeline, trigger_pipeline),
                started_at=run.started_at,
                duration_ms=_duration_ms(run.started_at, run.ended_at),
                step_count=run.step_count,
                cost_tokens=run.cost_tokens,
                stop_reason=run.stop_reason,
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
