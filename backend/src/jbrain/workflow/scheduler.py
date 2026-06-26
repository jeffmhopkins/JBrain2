"""The scheduler tick: fire due schedules and manual triggers onto the queue.

The engine's *dispatch* layer for time- and operator-driven work, sitting above
the proven `app.jobs` executor (docs/WORKFLOW_ENGINE_PLAN.md §5 Track B). It owns
no new execution machinery: a schedule's bound trigger resolves to a pipeline, the
pipeline's action steps name registered handlers (E3), and each step is enqueued
through the existing `queue.enqueue` exactly as a hardcoded trigger would. The
worker's claim loop, backoff, and dedup are untouched.

Two entry points, one resolution path:

- `scheduler_tick` claims schedules whose `next_run_at <= now` `FOR UPDATE SKIP
  LOCKED` (so a second worker is safe later, §7), enqueues each bound pipeline,
  and advances `next_run_at` **in app code** off the injected `now` — never a SQL
  `now()` — so a fake clock fully controls cadence in tests (N3). The advance is
  `next_schedule_run`: a fixed `interval` step (the reconcilers' sub-day cadence) or,
  for the task-style `on_demand`/`once`/`repeat` kinds (migration 0099), the same
  pure `jbrain.tasks.schedule.next_run_after` a task uses — so an owner can set a
  sweep's day/time like a task. Still no cron-string parser and no new dependency
  (§7, zero-new-dep goal): a `repeat` whose spec yields no next fire advances to a
  NULL `next_run_at`, dropping out of the due set until re-armed.
- `fire_trigger` enqueues a single trigger's pipeline immediately. It backs both
  the schedule path (a schedule fires its bound trigger) and the emergency
  "run now" Ops control (`POST /ops/triggers/{id}/run`), so a sweep is runnable
  without a restart (E4: re-firing is safe — the handlers keep their `has_active`
  dedup and write-once semantics).

Everything runs under `queue.SYSTEM_CTX`: scheduled/system work legitimately
crosses every firewall (the nightly sweeps touch all domains), and the run is
recorded as system-scoped rather than smuggling an escalation (E1). The narrowed
owner/agent-trigger scope path is Track A's event dispatcher, not built here.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.tasks.schedule import next_run_after, spec_from
from jbrain.workflow.contracts import Pipeline
from jbrain.workflow.registry import ActionRegistry, ActionSpec
from jbrain.workflow.runlog import EnqueuedStep, PipelineRunLog

log = structlog.get_logger()

# The deleted-note-artifact purge as a registered action so it can ride a nightly
# schedule like the predicate sweeps (Track B). It is NOT one of the shipped six
# in registry.ACTION_SPECS — that set is owned by the sibling action-registry task
# and is mirrored 1:1 by the app.actions seed (migration 0035) whose RLS test
# asserts an exact set match. So the purge action lives in-code only (the registry
# is the source of truth, the table its reference projection): the worker composes
# its registry from ACTION_SPECS + this spec, and a pipeline references it by name.
PURGE_ACTION = ActionSpec(
    name="purge_deleted_artifacts",
    version=1,
    handler="purge_deleted_artifacts",
    domain_optional=True,
    mutating=True,
    cost_class="cheap",
    dedup_key_expr=None,
    description="Reap chunks and blobs of deleted notes.",
)

# The two boot self-heal backfills as registered actions so they ride a recurring
# schedule + an emergency Ops trigger, not just boot (docs/WORKFLOW_ENGINE_PLAN.md
# §5 Wave 2 — the dropped-event safety net). Post-cutover a dropped best-effort
# event must not strand a note: the durability guarantee is the state columns
# (`notes.ingest_state='pending'`, `notes.integration_state <> 'integrated'`), and
# these sweeps are what reconcile them. Promoting them off boot-only means a
# dropped event self-heals within minutes, not at the next restart.
#
# Like PURGE_ACTION these live in-code only (the registry is the source of truth;
# the app.actions seed is its reference projection and its RLS test asserts an
# exact six-row set), so the worker composes ACTION_SPECS + (these three) and a
# pipeline references each by name. Both are cheap (a single bounded INSERT…SELECT
# over an indexed predicate) and re-firing is harmless: the SELECT excludes notes
# that already have an active job, so a second fire never double-enqueues (E4).
RECONCILE_PENDING_NOTES_ACTION = ActionSpec(
    name="reconcile_pending_notes",
    version=1,
    handler="reconcile_pending_notes",
    domain_optional=True,
    mutating=True,
    cost_class="cheap",
    dedup_key_expr=None,
    description="Re-enqueue ingest for notes still pending.",
)

RECONCILE_PENDING_INTEGRATION_ACTION = ActionSpec(
    name="reconcile_pending_integration",
    version=1,
    handler="reconcile_pending_integration",
    domain_optional=True,
    mutating=True,
    cost_class="cheap",
    dedup_key_expr=None,
    description="Re-enqueue integration for indexed-but-unintegrated notes.",
)

# The third boot self-heal backfill promoted off boot-only (Track S): a dropped
# `embed_note` enqueue strands a note's chunks unembedded until the next restart.
# Same in-code-only registration + idempotency contract as the two reconcilers
# above (the underlying SELECT excludes notes with an active embed_note job, so
# re-firing never double-enqueues, E4), so the worker composes it into its registry
# and a pipeline references it by name; it is NOT in the app.actions seed.
RECONCILE_UNEMBEDDED_NOTES_ACTION = ActionSpec(
    name="reconcile_unembedded_notes",
    version=1,
    handler="reconcile_unembedded_notes",
    domain_optional=True,
    mutating=True,
    cost_class="cheap",
    dedup_key_expr=None,
    description="Embed notes whose chunks slipped through.",
)

# The geofence reconciler backstop (Phase 7 Wave 3c): the scheduled twin of the
# inline detection at ingest. It rebuilds the place_geofence spatial mirror from
# the graph and re-evaluates each device subject's latest fix, healing a dropped
# projector hook or a dropped inline transition. Like the reconcilers above it is
# in-code only (not in the app.actions seed); a migration seeds its schedule +
# pipeline. Re-firing is idempotent (a fix already reflected in geofence_state
# re-evaluates to no crossing), so a recurring tick never double-emits (E4). It
# runs as the full owner — the only identity entitled to read every subject's
# pinned track — never a device-stamped job (B3).
GEOFENCE_SWEEP_ACTION = ActionSpec(
    name="geofence_sweep",
    version=1,
    handler="geofence_sweep",
    domain_optional=True,
    mutating=True,
    cost_class="cheap",
    dedup_key_expr=None,
    description="Rebuild geofence mirrors and re-detect missed transitions.",
)

# A monotonic UTC clock the tick reads through, so a test can inject a frozen one
# and prove next_run_at advances deterministically (no real timer, N3).
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def advance(now: datetime, interval_seconds: int) -> datetime:
    """The next fire time, computed app-side off the injected `now` (N3): one
    interval out from this fire. Fixed forward step, not catch-up — a backed-up
    tick schedules the next run one interval from *now*, never replays missed
    runs (the sweeps are idempotent, so a coalesced miss is harmless, and a
    catch-up storm after downtime would be worse than one skipped night)."""
    return now + timedelta(seconds=interval_seconds)


def next_schedule_run(
    *,
    now: datetime,
    schedule_kind: str,
    interval_seconds: int | None,
    schedule_freq: str | None,
    schedule_days: Sequence[int],
    schedule_time: str | None,
    run_at: datetime | None,
    timezone: str,
) -> datetime | None:
    """The next fire instant for a schedule, computed app-side off the injected
    `now` (N3) — the single advance contract for both the legacy interval kind and
    the task-style spec kinds (on_demand / once / repeat).

    `interval` is the fixed forward step the reconcilers ride (a sub-day cadence the
    task model can't express). The spec kinds reuse the exact pure
    `jbrain.tasks.schedule.next_run_after` a task uses, so a sweep set to "every
    weekday at 07:00" fires identically to a task with that schedule. Returns None
    when the schedule has no upcoming fire (on_demand, or a once whose moment has
    passed) — the tick stores that as a NULL next_run_at, removing it from the due
    set until it is re-armed."""
    if schedule_kind == "interval":
        # A legacy interval row always carries an interval_seconds (the column was
        # NOT NULL until this kind existed); guard defensively so a malformed row
        # falls out of the due set rather than crashing the tick.
        if interval_seconds is None:
            return None
        return advance(now, interval_seconds)
    spec = spec_from(
        kind=schedule_kind,
        freq=schedule_freq,
        days=schedule_days,
        time=schedule_time,
        run_at=run_at,
        tz=timezone,
    )
    return next_run_after(spec, now)


class ScheduleResolutionError(Exception):
    """A due schedule has no enabled trigger, or its trigger names a pipeline that
    does not exist / references an unregistered action. Surfaced (not swallowed) so
    a misconfigured schedule is diagnosable rather than silently skipped."""


@dataclass(frozen=True)
class FiredTrigger:
    """The audit record of one trigger firing: which pipeline ran and the ids of
    the jobs its steps enqueued (the value the Ops 'run now' control returns)."""

    trigger_id: str
    pipeline: str
    job_ids: list[str]


async def _load_pipeline(session: AsyncSession, name: str) -> Pipeline:
    """The newest version of a pipeline definition by name. A pipeline is
    addressed by name across versions (the table PK is composite); a definition
    change is a new version, so the highest version is the live one."""
    row = (
        await session.execute(
            text(
                "SELECT name, version, steps::text AS steps, description"
                " FROM app.pipelines WHERE name = :name"
                " ORDER BY version DESC LIMIT 1"
            ),
            {"name": name},
        )
    ).first()
    if row is None:
        raise ScheduleResolutionError(f"no pipeline named {name!r}")
    return Pipeline(
        name=row.name,
        version=row.version,
        steps=json.loads(row.steps),
        description=row.description,
    )


async def _enqueue_pipeline(
    maker: async_sessionmaker[AsyncSession],
    registry: ActionRegistry,
    pipeline: Pipeline,
) -> list[EnqueuedStep]:
    """Enqueue one job per pipeline step through the existing queue (E3: every
    step names a registered action; an unknown action is config drift and fails
    the fire loudly rather than enqueuing a job no handler can run). The job
    `kind` is the action's handler key, identical to what a hardcoded trigger
    enqueues today — the scheduler is a different *trigger*, not a new executor.
    Returns one EnqueuedStep per enqueued job so the caller can record the run."""
    # Validate every step resolves BEFORE enqueuing any, so a bad later step never
    # leaves a half-enqueued run (a pipeline fires all-or-nothing).
    for step in pipeline.steps:
        spec = registry.get(step.action)  # raises ActionRegistryError on drift
        if spec.version != step.action_version:
            raise ScheduleResolutionError(
                f"pipeline {pipeline.name!r} pins action {step.action!r}"
                f" v{step.action_version}, registry has v{spec.version}"
            )
    # enqueue opens its own scoped session per job (the queue's contract).
    steps: list[EnqueuedStep] = []
    for step in pipeline.steps:
        spec = registry.get(step.action)
        # A preconditioned action can sit DEFERRED in the queue (status='queued' with a
        # future run_after, waiting on its precondition — e.g. a model to load). Firing
        # it again on the next tick would stack a second waiting job, so COALESCE: when
        # one is already active (queued or running), enqueue nothing and let the waiting
        # job absorb this fire's intent. These are payloadless system sweeps, so the
        # pending job and this one are interchangeable. A plain action enqueues as
        # before — re-firing it is handler-dedup-safe (E4).
        if spec.precondition and await queue.has_active_kind(
            maker, queue.SYSTEM_CTX, spec.handler
        ):
            log.info("scheduler.step_coalesced", action=step.action, kind=spec.handler)
            continue
        job_id = await queue.enqueue(maker, queue.SYSTEM_CTX, spec.handler, step.params)
        steps.append(EnqueuedStep(kind=spec.handler, job_id=job_id))
    return steps


async def fire_trigger(
    maker: async_sessionmaker[AsyncSession],
    registry: ActionRegistry,
    trigger_id: str,
    *,
    require_manual: bool = False,
) -> FiredTrigger:
    """Enqueue the pipeline bound to a single trigger, now. The shared resolution
    path for a schedule firing and the emergency Ops control; idempotent to re-run
    (E4) — the enqueued handlers keep their own dedup. Raises
    ScheduleResolutionError if the trigger is unknown/disabled or its pipeline
    can't resolve. `require_manual` gates the Ops emergency path to `manual` triggers
    only (the scheduler tick fires schedule-bound triggers regardless)."""
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        row = (
            await session.execute(
                text("SELECT pipeline, enabled, manual FROM app.triggers WHERE id = :id"),
                {"id": trigger_id},
            )
        ).first()
    if row is None:
        raise ScheduleResolutionError(f"no trigger {trigger_id!r}")
    if not row.enabled:
        raise ScheduleResolutionError(f"trigger {trigger_id!r} is disabled")
    if require_manual and not row.manual:
        raise ScheduleResolutionError(f"trigger {trigger_id!r} is not manually fireable")
    async with scoped_session(maker, queue.SYSTEM_CTX) as session:
        pipeline = await _load_pipeline(session, row.pipeline)
    steps = await _enqueue_pipeline(maker, registry, pipeline)
    # Record the dispatch on the unified run-log so a schedule/Ops fire is auditable
    # from app.runs — exactly like the event-triggered path (dispatcher.live_enqueue)
    # and the agent loop. Without this, manual "Run now" and nightly sweeps enqueued
    # real jobs but left no run row, so the Ops Runs surface showed nothing. The run
    # is `done` on write (the dispatch's job is to enqueue; each step's job carries
    # its own status), and `system` since a trigger fire runs under SYSTEM_CTX.
    run_id = await PipelineRunLog(maker).record(
        queue.SYSTEM_CTX,
        pipeline=pipeline.name,
        trigger_id=trigger_id,
        ran_as="system",
        domain_code=None,
        principal_id=None,
        steps=steps,
    )
    job_ids = [step.job_id for step in steps]
    log.info(
        "scheduler.trigger_fired",
        trigger_id=trigger_id,
        pipeline=pipeline.name,
        run_id=run_id,
        job_ids=job_ids,
    )
    return FiredTrigger(trigger_id=trigger_id, pipeline=pipeline.name, job_ids=job_ids)


async def scheduler_tick(
    maker: async_sessionmaker[AsyncSession],
    registry: ActionRegistry,
    *,
    now: datetime | None = None,
) -> list[FiredTrigger]:
    """Claim every due, enabled schedule, fire its bound trigger, and advance its
    `next_run_at` app-side.

    A schedule is due when `next_run_at <= now` and `enabled`. Claimed `FOR UPDATE
    SKIP LOCKED` so two workers never double-fire one schedule (a second worker is
    a future possibility, §7) and a slow fire never blocks an unrelated schedule.
    `next_run_at` advances to `now + interval` computed HERE in Python off the
    injected `now`, never SQL `now()`, so a frozen test clock fully determines
    cadence (N3); `last_run_at` records the fire instant. A schedule with no
    enabled trigger is advanced anyway and logged — a dangling schedule must not
    wedge the tick by staying perpetually due.
    """
    moment = now or utcnow()
    fired: list[FiredTrigger] = []
    # One claim transaction per schedule: claim + advance commit together so the
    # schedule leaves the due set atomically, then the (independent) enqueue runs
    # outside the lock. Re-querying the due set each pass drains all due rows.
    while True:
        async with scoped_session(maker, queue.SYSTEM_CTX) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT s.id, s.interval_seconds, s.schedule_kind,
                               s.schedule_freq, s.schedule_days, s.schedule_time,
                               s.run_at, s.timezone,
                               t.id AS trigger_id, t.pipeline
                        FROM app.schedules s
                        LEFT JOIN app.triggers t
                          ON t.on_schedule_id = s.id AND t.enabled
                        WHERE s.enabled AND s.next_run_at <= :now
                        ORDER BY s.next_run_at
                        FOR UPDATE OF s SKIP LOCKED
                        LIMIT 1
                        """
                    ),
                    {"now": moment},
                )
            ).first()
            if row is None:
                return fired
            next_run = next_schedule_run(
                now=moment,
                schedule_kind=row.schedule_kind,
                interval_seconds=row.interval_seconds,
                schedule_freq=row.schedule_freq,
                schedule_days=row.schedule_days or (),
                schedule_time=row.schedule_time,
                run_at=row.run_at,
                timezone=row.timezone,
            )
            await session.execute(
                text(
                    "UPDATE app.schedules"
                    " SET last_run_at = :now, next_run_at = :next WHERE id = :id"
                ),
                {"now": moment, "next": next_run, "id": str(row.id)},
            )
            schedule_id = str(row.id)
            trigger_id = str(row.trigger_id) if row.trigger_id is not None else None
        # Outside the claim transaction: the schedule is already advanced, so a
        # transient enqueue failure re-surfaces it only at the NEXT due time, not
        # immediately — the same at-most-occasionally-skip posture as the queue.
        if trigger_id is None:
            log.warning("scheduler.schedule_no_trigger", schedule_id=schedule_id)
            continue
        try:
            fired.append(await fire_trigger(maker, registry, trigger_id))
        except (ScheduleResolutionError, queue.PermanentJobError) as exc:
            log.error(
                "scheduler.fire_failed",
                schedule_id=schedule_id,
                trigger_id=trigger_id,
                error=repr(exc),
            )


# How often the worker loop runs the tick. The schedules themselves are nightly;
# this is just the resolution that the cheap due-query is polled at. Kept well
# below the smallest interval so a due schedule fires within a minute.
TICK_SECONDS = 30.0


async def run_tick_safely(
    maker: async_sessionmaker[AsyncSession], registry: ActionRegistry
) -> None:
    """Run one tick, swallowing failures so a scheduler blip never kills the
    worker loop (mirrors the loop's own DB-blip tolerance). The worker calls this
    on its cadence (worker.run_loop)."""
    try:
        await scheduler_tick(maker, registry)
    except Exception as exc:  # noqa: BLE001 - the tick must not crash the worker
        log.warning("scheduler.tick_error", error=repr(exc))


def purge_handler(
    maker: async_sessionmaker[AsyncSession],
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Wrap the boot purge sweep as a queue handler so it is fireable as an action
    (it takes no payload — the sweep finds its own candidates)."""
    from jbrain.analysis import purge

    async def handler(_payload: dict[str, Any]) -> None:
        await purge.backfill_deleted_note_artifacts(maker)

    return handler


def reconcile_pending_notes_handler(
    maker: async_sessionmaker[AsyncSession],
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Wrap the pending-ingest backfill as a queue handler so it is fireable as a
    recurring schedule + an emergency Ops trigger, not just at boot. It takes no
    payload — the sweep finds its own candidates (every note in `ingest_state =
    'pending'` lacking an active `ingest_note` job) — and runs under SYSTEM_CTX
    because reconciliation legitimately crosses every domain (E1). Re-firing is
    safe: the underlying INSERT…SELECT skips notes that already have an active job,
    so a dropped-event re-run enqueues nothing extra (E4)."""

    async def handler(_payload: dict[str, Any]) -> None:
        await queue.backfill_pending_notes(maker, queue.SYSTEM_CTX)

    return handler


def reconcile_pending_integration_handler(
    maker: async_sessionmaker[AsyncSession],
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Wrap the pending-integration backfill as a queue handler (bounded,
    oldest-first), fireable on a recurring schedule + on demand from Ops. Same
    SYSTEM_CTX + idempotency contract as the pending-notes reconciler: the
    INSERT…SELECT skips notes with an active `integrate_note` job, so re-firing
    never double-enqueues (E4)."""

    async def handler(_payload: dict[str, Any]) -> None:
        await queue.backfill_pending_integration(maker, queue.SYSTEM_CTX)

    return handler


def reconcile_unembedded_notes_handler(
    maker: async_sessionmaker[AsyncSession],
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Wrap the unembedded-notes backfill as a queue handler (Track S), fireable on
    a recurring schedule + on demand from Ops, not just at boot. Same SYSTEM_CTX +
    idempotency contract as the other reconcilers: the INSERT…SELECT enqueues
    `embed_note` for notes with NULL-embedding chunks but skips any with an active
    `embed_note` job, so a dropped-event re-run never double-enqueues (E4)."""

    async def handler(_payload: dict[str, Any]) -> None:
        await queue.backfill_unembedded_notes(maker, queue.SYSTEM_CTX)

    return handler


def geofence_sweep_handler(
    maker: async_sessionmaker[AsyncSession],
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Wrap the geofence reconciler as a queue handler, fireable on a recurring
    schedule + on demand from Ops. It takes no payload — the sweep finds its own
    work (every Place's live geofence fact + every device subject's latest fix) —
    and runs as the full owner inside `sweep_geofences` (the only identity entitled
    to reconcile across every subject's pinned track, B3). Idempotent: a stream the
    inline path already handled re-evaluates to no crossing, so re-firing emits
    nothing extra (E4)."""

    async def handler(_payload: dict[str, Any]) -> None:
        from jbrain.locations.geofence import sweep_geofences

        await sweep_geofences(maker)

    return handler
