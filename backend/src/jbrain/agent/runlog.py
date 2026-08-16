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

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, and_, bindparam, not_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import Run, RunStep
from jbrain.models.workflow import Trigger
from jbrain.queue import queued_depth

log = structlog.get_logger()

# A margin above the hard turn wall-clock (`_MAX_TURN_WALL_CLOCK_S`, 3600s, in
# api/agent.py): a genuinely-live detached turn is force-ended and settled by then, so any
# agent/subagent run still 'running' past this is provably orphaned. Sized above the
# ceiling + a margin so the periodic sweep can never race a real turn.
STRANDED_AFTER_SECONDS = 3900
# How often the background sweep runs. The boot reaper clears the pre-restart backlog once
# on startup; this only bounds accumulation between restarts, so it can be infrequent.
REAP_INTERVAL_SECONDS = 900


class AgentRunLog:
    """CRUD for the agent run log, on owner-scoped sessions (runs are owner-only)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def start(
        self,
        ctx: SessionContext,
        *,
        session_id: str,
        prompt_version: str,
        kind: str = "agent",
        parent_run_id: str | None = None,
        call_stamp: dict[str, Any] | None = None,
    ) -> str:
        """Open a RUNNING run row and return its id.

        `call_stamp` records what this run was asked to do — model, provider, effort,
        context window, tools, persona, the triggering message (migration 0166). It is
        resolved at turn start and was otherwise discarded, so the vitals detail surface
        would have had to guess which model is answering. Optional: a driver that
        doesn't stamp leaves it NULL and the surface renders what it has."""
        async with scoped_session(self._maker, ctx) as session:
            # kind='agent' is explicit so the shared run log's CHECK admits this row
            # (it requires session_id + prompt_version for agent runs). A spawned
            # child run is kind='subagent' with parent_run_id set for the tree cost
            # rollup (docs/archive/SUBAGENT_SPAWNING_PLAN.md); it still carries its own
            # session + prompt version.
            run = Run(
                kind=kind,
                session_id=uuid.UUID(session_id),
                prompt_version=prompt_version,
                parent_run_id=uuid.UUID(parent_run_id) if parent_run_id else None,
                call_stamp=call_stamp,
            )
            session.add(run)
            await session.flush()
            return str(run.id)

    async def stamp(self, ctx: SessionContext, run_id: str, call_stamp: dict[str, Any]) -> None:
        """Record what the run was asked to do, once the route is resolved.

        A separate write from `start` because the run row is opened before the model is
        chosen — effort, context window and vision support are all awaited off the
        router afterwards. Reordering the turn's startup to stamp inside that INSERT
        would leave no run row while they resolve, losing any turn that failed during
        resolution. Best-effort by design: the caller swallows errors, because a
        provenance blob must never take a turn down with it."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(Run).where(Run.id == uuid.UUID(run_id)).values(call_stamp=call_stamp)
            )

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

    async def reap_stranded(
        self,
        ctx: SessionContext,
        *,
        older_than_seconds: float | None = None,
    ) -> int:
        """Close agent/subagent run rows stuck at 'running' whose `finish()` never landed.

        A turn whose process died (crash/OOM/SIGKILL, or a graceful drain that overran its
        bound) or a child cancelled in a gap before it could settle leaves its row 'running'
        forever — inflating the Runs 'active now' tile and leaving the sub-agent rail unable
        to tell a live turn from a dead one. This marks such rows `status='error'`,
        `stop_reason='stranded'`, `ended_at=now()` (the constraint-valid terminal the runs
        table and the frontend RunStatus already carry).

        `older_than_seconds` bounds the sweep to rows at least that old: the periodic sweep
        passes a margin above the hard turn wall-clock so it can never race a genuinely-live
        detached turn, while the boot reaper passes None — a fresh process owns no prior
        'running' row, so every one is a pre-restart orphan. Owner/system-scoped like the
        rest of the log (runs are owner-only RLS). Returns the count closed."""
        clause = ""
        params: dict[str, object] = {}
        if older_than_seconds is not None:
            clause = " AND started_at < now() - make_interval(secs => :secs)"
            params["secs"] = older_than_seconds
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                text(
                    "UPDATE app.runs SET status = 'error', stop_reason = 'stranded',"
                    " ended_at = now(), progress_note = NULL"
                    " WHERE kind IN ('agent', 'subagent') AND status = 'running'" + clause
                ),
                params,
            )
        return cast(CursorResult[Any], result).rowcount or 0

    def bound(self, ctx: SessionContext, run_id: str) -> "BoundRecorder":
        """A `RunRecorder` (loop.py) pinned to one run and context."""
        return BoundRecorder(self, ctx, run_id)


async def reap_stranded_loop(
    runlog: "AgentRunLog",
    ctx: SessionContext,
    *,
    interval_seconds: int = REAP_INTERVAL_SECONDS,
    older_than_seconds: int = STRANDED_AFTER_SECONDS,
) -> None:
    """Sweep stranded agent/subagent runs forever, sleeping `interval_seconds` between
    passes. The boot reaper clears the pre-restart backlog once; this only bounds
    accumulation while the process stays up (a child stranded by a rare double-cancel, say).
    A sweep failure is logged and the loop continues (a transient DB hiccup must not kill
    the reaper); cancellation propagates so shutdown can stop it cleanly. Sleeps BEFORE the
    first sweep — the boot reaper already cleared the pre-restart backlog, so nothing needs an
    immediate pass, and no age-eligible row can exist until at least one interval has passed."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            reaped = await runlog.reap_stranded(ctx, older_than_seconds=older_than_seconds)
            if reaped:
                log.info("agent.runlog.reaped_stranded", reaped=reaped)
        except Exception:
            log.exception("agent.runlog.reaper_failed")


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


# The scheduler's seeded background-maintenance sweeps (workflow/scheduler.py):
# reconcile_* + the named ones — high-frequency, ~0-token housekeeping the Runs
# surface can filter out. Mirrored by the frontend's isSweep predicate.
_SWEEP_NAMES = ("geofence_sweep", "purge_deleted_artifacts")
# Raw-SQL form of the same predicate, for the stats aggregates.
_SWEEP_SQL = (
    "kind = 'pipeline' AND (pipeline LIKE 'reconcile\\_%' ESCAPE '\\'"
    " OR pipeline IN ('geofence_sweep', 'purge_deleted_artifacts'))"
)


def _sweep_filter():  # type: ignore[no-untyped-def]
    """ORM form of the sweep predicate for `list_recent`'s exclude_sweeps."""
    return and_(
        Run.kind == "pipeline",
        or_(Run.pipeline.like("reconcile\\_%", escape="\\"), Run.pipeline.in_(_SWEEP_NAMES)),
    )


def _chip_bucket(kind: str) -> str:
    """Map a stored run kind to its Runs-surface chip bucket: a subagent run rides
    the Agent chip; anything unrecognized buckets with pipeline (mirrors the frontend)."""
    if kind in ("agent", "subagent"):
        return "agent"
    if kind == "integration":
        return "integration"
    return "pipeline"


@dataclass(frozen=True)
class RunStats:
    """The Runs dashboard's tile + chip-count aggregates, computed over the whole log
    (not just the fetched page — so `tokens_today` reflects the day, not the last N
    rows). The three tiles are always today (UTC day, matching the usage reader) / now;
    the per-kind counts respect the surface's active date-range + hide-sweeps so the
    chip pills match the filtered list."""

    active: int
    failed_today: int
    tokens_today: int
    # Keyed by chip bucket: agent | integration | pipeline.
    by_kind: dict[str, int]


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
class LiveTurnRow:
    """One run that is in flight right now, for the vitals detail roster.

    Read from `app.runs` rather than the in-process live-turn registry on purpose: that
    registry holds only parent /chat turns (api/agent.py), so a deep-research fan would
    collapse to a single row and a workflow run would not appear at all. The table sees
    every kind, carries the parent link that nests a fan's children, and survives an API
    restart that would empty the registry."""

    id: str
    kind: str
    status: str
    name: str
    started_at: datetime
    elapsed_ms: int
    step_count: int
    cost_tokens: int
    progress_note: str | None
    parent_run_id: str | None
    session_id: str | None
    domain_code: str | None
    ran_as: str
    prompt_version: str | None
    # What triggered it: the trigger's pipeline name for an engine run, else None —
    # which the surface reads as "the owner started this".
    trigger_pipeline: str | None
    # The resolved call (migration 0166): model, provider, effort, window, tools,
    # persona, opening message. None for a run whose driver doesn't stamp, and for
    # every run that predates the column.
    call_stamp: dict[str, Any] | None


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

    async def list_live(self, ctx: SessionContext, *, limit: int = 50) -> list[LiveTurnRow]:
        """Every run in flight right now, oldest first so a parent precedes the children
        it spawned. RLS-scoped like every other read here.

        Ordering is by start time rather than by tree: a fan's children are always
        started after their parent, so oldest-first already yields a renderable order
        without a recursive query, and `parent_run_id` lets the surface nest them."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(Run, Trigger.pipeline)
                    .outerjoin(Trigger, Run.trigger_id == Trigger.id)
                    .where(Run.status == "running")
                    .order_by(Run.started_at)
                    .limit(limit)
                )
            ).all()
            now = datetime.now(tz=UTC)
            return [
                LiveTurnRow(
                    id=str(run.id),
                    kind=run.kind,
                    status=run.status,
                    name=self._display_name(run.kind, run.pipeline, trigger_pipeline),
                    started_at=run.started_at,
                    elapsed_ms=max(0, int((now - run.started_at).total_seconds() * 1000)),
                    step_count=run.step_count,
                    cost_tokens=run.cost_tokens,
                    progress_note=run.progress_note,
                    parent_run_id=str(run.parent_run_id) if run.parent_run_id else None,
                    session_id=str(run.session_id) if run.session_id else None,
                    domain_code=run.domain_code,
                    ran_as=run.ran_as,
                    prompt_version=run.prompt_version,
                    trigger_pipeline=trigger_pipeline,
                    call_stamp=run.call_stamp,
                )
                for run, trigger_pipeline in rows
            ]

    async def list_recent(
        self,
        ctx: SessionContext,
        *,
        limit: int = 50,
        kinds: list[str] | None = None,
        exclude_sweeps: bool = False,
        since: datetime | None = None,
    ) -> list[RunSummary]:
        """The run log, newest first, filtered server-side so the Runs surface can
        reach past the recency window: `kinds` (the enabled chip kinds — subagent
        rides under agent), `exclude_sweeps` (drop the reconcile housekeeping), and
        `since` (the date-range floor). Without filters this is the plain recent page."""
        async with scoped_session(self._maker, ctx) as session:
            stmt = select(Run, Trigger.pipeline).outerjoin(Trigger, Run.trigger_id == Trigger.id)
            if kinds:
                stmt = stmt.where(Run.kind.in_(kinds))
            if exclude_sweeps:
                stmt = stmt.where(not_(_sweep_filter()))
            if since is not None:
                stmt = stmt.where(Run.started_at >= since)
            rows = (await session.execute(stmt.order_by(Run.started_at.desc()).limit(limit))).all()
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

    async def stats(
        self,
        ctx: SessionContext,
        *,
        since: datetime | None = None,
        exclude_sweeps: bool = False,
    ) -> RunStats:
        """Tile + chip-count aggregates over the whole log. The tiles (active now /
        failed today / tokens today) are computed independently of the surface's
        filters; the per-kind counts respect `since` + `exclude_sweeps` so the chip
        pills agree with the filtered list. Owner-scoped like the rest of the reader."""
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with scoped_session(self._maker, ctx) as session:
            tiles = (
                await session.execute(
                    text(
                        "SELECT"
                        "  count(*) FILTER (WHERE status = 'running') AS active,"
                        "  count(*) FILTER (WHERE status = 'error' AND started_at >= :day)"
                        "    AS failed_today,"
                        "  COALESCE(sum(cost_tokens) FILTER (WHERE started_at >= :day), 0)"
                        "    AS tokens_today"
                        " FROM app.runs"
                    ),
                    {"day": day_start},
                )
            ).one()
            clauses: list[str] = []
            params: dict[str, object] = {}
            if since is not None:
                clauses.append("started_at >= :since")
                params["since"] = since
            if exclude_sweeps:
                clauses.append(f"NOT ({_SWEEP_SQL})")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            kind_rows = (
                await session.execute(
                    text(f"SELECT kind, count(*) AS n FROM app.runs{where} GROUP BY kind"), params
                )
            ).all()
            by_kind = {"agent": 0, "integration": 0, "pipeline": 0}
            for kind, n in kind_rows:
                by_kind[_chip_bucket(kind)] += int(n)
            return RunStats(
                active=int(tiles.active),
                failed_today=int(tiles.failed_today),
                tokens_today=int(tiles.tokens_today),
                by_kind=by_kind,
            )

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
