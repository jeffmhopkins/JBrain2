"""The Automations operator surface reader (the Ops "Workflow" screen,
docs/mocks/workflow-ops-a-automations-list.html).

Projects the live engine config — `app.triggers` joined to its `app.schedules`
and the newest `app.pipelines` version it names — into the "when X -> run Y"
cards the screen renders, each with a recent-run summary drawn from the same
`runs` log the Runs surface reads. A second view lists the action registry (the
Catalog tab), flagging which actions are mirrored into `app.actions` vs in-code
only.

All reads run on an RLS-scoped session (CLAUDE.md rule 3): triggers/schedules/
pipelines/actions are owner-system reference data, so a non-owner session reads
nothing — the firewall, not this code, is the enforcement point. This reader only
READS; the enable/disable mutation and the run-now fire live in the Ops API.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.runlog import _duration_ms
from jbrain.db.session import SessionContext, scoped_session
from jbrain.workflow.registry import ActionRegistry
from jbrain.workflow.scheduler import next_schedule_run

# The three groups the surface renders, in order — by SUBJECT, not by how a trigger
# fires. `note` is the whole note lifecycle (ingest/integrate + their reconcilers +
# inbox triage), `wiki` the wiki builder/lint sweeps, `maintenance` the background
# graph/data hygiene. An automation's group is its pipeline's primary action's
# `category` (registry.ActionSpec.category), so a new automation slots itself by
# declaring a category — never a hardcoded id list. The default category is
# `maintenance`, so an unclassified action falls to the bottom bucket rather than
# vanishing. (Cadence still drives the schedule EDITOR mode — that's a separate,
# frontend concern read off `interval_seconds`, not the display group.)
GROUP_NOTE = "note"
GROUP_WIKI = "wiki"
GROUP_MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class StepView:
    """One ordered pipeline step, resolved through the registry so the surface can
    render its cost class + description without a second lookup. `known` is False
    when a pipeline names an action the (worker-equivalent) registry does not carry
    — config drift surfaced honestly rather than a blank chip."""

    action: str
    cost_class: str
    description: str
    known: bool


@dataclass(frozen=True)
class RecentRunView:
    """A run-log row for a pipeline, as the expanded card renders it: status, when,
    how long, and a failed run's first-error hint."""

    id: str
    status: str
    started_at: datetime
    duration_ms: int | None
    last_error: str | None


@dataclass(frozen=True)
class AutomationView:
    """One "when -> do" card: a trigger, what fires it, the pipeline it runs (its
    ordered steps), whether it is enabled + manually fireable, and a recent-run
    summary. `kind` is on_event | schedule; `group` buckets it for the mock's
    sections."""

    trigger_id: str
    kind: str  # on_event | schedule
    group: str
    pipeline: str
    enabled: bool
    manual: bool
    steps: list[StepView]
    recent_runs: list[RecentRunView]
    # Event-bound: the event type that fires it (the `when X`). None for schedules.
    on_event: str | None = None
    # Schedule-bound: the schedule the trigger fires off, and the cadence +
    # next/last fire instants the meta line reads. schedule_id powers the
    # enable/disable of the schedule itself (the mock toggles both). None for event.
    schedule_id: str | None = None
    interval_seconds: int | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    # The task-style schedule spec (migration 0099) the owner edits on a sweep card:
    # kind=interval keeps the legacy fixed cadence; on_demand/once/repeat mirror a
    # task's day/time schedule. All None/empty for an event trigger.
    schedule_kind: str | None = None
    schedule_freq: str | None = None
    schedule_days: list[int] = field(default_factory=list)
    schedule_time: str | None = None
    run_at: datetime | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class ActionView:
    """A Catalog row: a registered action's name, cost class, blast-radius flags,
    its one-line description, and whether `app.actions` carries a seed row for it
    (vs the in-code-only sweeps/eval)."""

    name: str
    cost_class: str
    domain_optional: bool
    mutating: bool
    description: str
    seeded: bool


@dataclass(frozen=True)
class AutomationsView:
    """The full Automations payload: the grouped cards + the action catalog."""

    automations: list[AutomationView] = field(default_factory=list)
    actions: list[ActionView] = field(default_factory=list)


# How many recent runs to surface per automation in the expanded card — the mock
# shows a short list, not the full history (the Runs surface owns that).
_RECENT_PER_PIPELINE = 5


class AutomationsReader:
    """Owner-scoped reads of the engine config for the Automations surface. The
    `registry` is the worker-equivalent action set (the shipped six + the in-code
    sweeps + eval) so a pipeline step resolves to the same cost/description the
    worker would run; `seeded_names` is the subset projected into `app.actions`."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        registry: ActionRegistry,
        seeded_names: frozenset[str],
    ):
        self._maker = maker
        self._registry = registry
        self._seeded = seeded_names

    def _group(self, steps: list[StepView]) -> str:
        """Bucket an automation by its pipeline's primary (first registered) action's
        category. A pipeline whose steps are all unknown to the registry (config
        drift) falls to maintenance — the safe bottom bucket — rather than vanishing
        from the surface."""
        for step in steps:
            if step.action in self._registry:
                return self._registry.get(step.action).category
        return GROUP_MAINTENANCE

    def _resolve_steps(self, steps: list[dict]) -> list[StepView]:
        out: list[StepView] = []
        for step in steps:
            action = str(step.get("action", ""))
            if action in self._registry:
                spec = self._registry.get(action)
                out.append(
                    StepView(
                        action=action,
                        cost_class=spec.cost_class,
                        description=spec.description,
                        known=True,
                    )
                )
            else:
                # A pipeline naming an unregistered action is drift; render it as a
                # known-unknown rather than hiding it, so the operator sees it.
                out.append(
                    StepView(action=action, cost_class="standard", description="", known=False)
                )
        return out

    async def load(self, ctx: SessionContext) -> AutomationsView:
        async with scoped_session(self._maker, ctx) as session:
            automations = await self._load_automations(session)
        return AutomationsView(automations=automations, actions=self._load_actions())

    async def _load_automations(self, session: AsyncSession) -> list[AutomationView]:
        # One pass: every trigger with its schedule (if any) and the steps of the
        # newest version of the pipeline it names. The lateral pulls only the live
        # pipeline version (the engine resolves by name, highest version wins —
        # scheduler._load_pipeline). Schedule-less (event) triggers keep NULL sched
        # columns via the LEFT JOIN.
        rows = (
            await session.execute(
                text(
                    """
                    SELECT t.id::text AS trigger_id,
                           t.on_event,
                           t.pipeline,
                           t.enabled,
                           t.manual,
                           s.id::text AS schedule_id,
                           s.interval_seconds,
                           s.next_run_at,
                           s.last_run_at,
                           s.schedule_kind,
                           s.schedule_freq,
                           s.schedule_days,
                           s.schedule_time,
                           s.run_at,
                           s.timezone,
                           coalesce(p.steps, '[]'::jsonb)::text AS steps
                    FROM app.triggers t
                    LEFT JOIN app.schedules s ON s.id = t.on_schedule_id
                    LEFT JOIN LATERAL (
                        SELECT steps FROM app.pipelines
                        WHERE name = t.pipeline
                        ORDER BY version DESC LIMIT 1
                    ) p ON true
                    ORDER BY (t.on_event IS NULL), s.interval_seconds, t.pipeline
                    """
                )
            )
        ).all()

        out: list[AutomationView] = []
        for row in rows:
            kind = "on_event" if row.on_event is not None else "schedule"
            steps = self._resolve_steps(json.loads(row.steps))
            recent = await self._recent_runs(session, row.trigger_id, row.pipeline)
            out.append(
                AutomationView(
                    trigger_id=row.trigger_id,
                    kind=kind,
                    group=self._group(steps),
                    pipeline=row.pipeline,
                    enabled=row.enabled,
                    manual=row.manual,
                    steps=steps,
                    recent_runs=recent,
                    on_event=row.on_event,
                    schedule_id=row.schedule_id,
                    interval_seconds=row.interval_seconds,
                    next_run_at=row.next_run_at,
                    last_run_at=row.last_run_at,
                    schedule_kind=row.schedule_kind,
                    schedule_freq=row.schedule_freq,
                    schedule_days=list(row.schedule_days) if row.schedule_days else [],
                    schedule_time=row.schedule_time,
                    run_at=row.run_at,
                    timezone=row.timezone,
                )
            )
        return out

    async def _recent_runs(
        self, session: AsyncSession, trigger_id: str, pipeline: str
    ) -> list[RecentRunView]:
        # The recent runs for this automation: prefer the runs the dispatcher/
        # scheduler stamped with this trigger_id; fall back to runs naming the same
        # pipeline (a manually fired sweep records the pipeline). Reuses the shared
        # `runs` log the Runs surface reads. last_error is the first failing step's
        # name (same projection as RunLogReader), so a failed card shows why.
        rows = (
            await session.execute(
                text(
                    """
                    SELECT r.id::text AS id, r.status, r.started_at, r.ended_at,
                           (SELECT rs.name FROM app.run_steps rs
                            WHERE rs.run_id = r.id AND rs.ok = false
                            ORDER BY rs.idx LIMIT 1) AS first_error
                    FROM app.runs r
                    WHERE r.trigger_id = cast(:tid AS uuid) OR r.pipeline = :pipeline
                    ORDER BY r.started_at DESC
                    LIMIT :limit
                    """
                ),
                {"tid": trigger_id, "pipeline": pipeline, "limit": _RECENT_PER_PIPELINE},
            )
        ).all()
        return [
            RecentRunView(
                id=row.id,
                status=row.status,
                started_at=row.started_at,
                duration_ms=_duration_ms(row.started_at, row.ended_at),
                last_error=row.first_error if row.status == "error" else None,
            )
            for row in rows
        ]

    def _load_actions(self) -> list[ActionView]:
        # The catalog is the registry itself (in-code source of truth), sorted by
        # name; `seeded` flags the subset mirrored into app.actions.
        out: list[ActionView] = []
        for name in sorted(self._registry.names()):
            spec = self._registry.get(name)
            out.append(
                ActionView(
                    name=spec.name,
                    cost_class=spec.cost_class,
                    domain_optional=spec.domain_optional,
                    mutating=spec.mutating,
                    description=spec.description,
                    seeded=spec.name in self._seeded,
                )
            )
        return out

    async def set_trigger_enabled(
        self, ctx: SessionContext, trigger_id: str, enabled: bool
    ) -> bool:
        """Toggle a trigger's `enabled` flag — the emergency-stop / re-arm control.
        Owner-scoped (the caller passes the owner ctx; the RLS UPDATE policy is the
        real gate). Returns False when no such trigger exists in scope (a 404 for
        the API), so a bad id never silently no-ops as success."""
        try:
            uuid.UUID(trigger_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                text("UPDATE app.triggers SET enabled = :enabled WHERE id = cast(:id AS uuid)"),
                {"enabled": enabled, "id": trigger_id},
            )
        return (cast(CursorResult[Any], result).rowcount or 0) > 0

    async def set_schedule_enabled(
        self, ctx: SessionContext, schedule_id: str, enabled: bool
    ) -> bool:
        """Toggle a schedule's `enabled` flag (a disabled schedule stops the
        scheduler tick from firing it). Same owner-scoped contract as
        set_trigger_enabled; returns False on an unknown id."""
        try:
            uuid.UUID(schedule_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                text("UPDATE app.schedules SET enabled = :enabled WHERE id = cast(:id AS uuid)"),
                {"enabled": enabled, "id": schedule_id},
            )
        return (cast(CursorResult[Any], result).rowcount or 0) > 0

    async def update_schedule(
        self,
        ctx: SessionContext,
        schedule_id: str,
        *,
        schedule_kind: str,
        interval_seconds: int | None,
        schedule_freq: str | None,
        schedule_days: Sequence[int],
        schedule_time: str | None,
        run_at: datetime | None,
        timezone: str,
        now: datetime | None = None,
    ) -> bool:
        """Replace a schedule's timing spec and recompute its `next_run_at` from it —
        the owner's "set this sweep's cadence like a task" control. `next_run_at` is
        derived through the same `next_schedule_run` the tick uses, so the editor and
        the scheduler never disagree on the next fire (an on_demand / spent-once spec
        recomputes to NULL, dropping it from the due set). Owner-scoped (the caller
        passes the owner ctx; the RLS UPDATE policy is the real gate). Returns False
        on a malformed or out-of-scope id, so a bad id never silently succeeds."""
        try:
            uuid.UUID(schedule_id)
        except ValueError:
            return False
        moment = now or datetime.now(UTC)
        next_run = next_schedule_run(
            now=moment,
            schedule_kind=schedule_kind,
            interval_seconds=interval_seconds,
            schedule_freq=schedule_freq,
            schedule_days=schedule_days,
            schedule_time=schedule_time,
            run_at=run_at,
            timezone=timezone,
        )
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                text(
                    "UPDATE app.schedules SET"
                    " schedule_kind = :kind, interval_seconds = :interval,"
                    " schedule_freq = :freq, schedule_days = :days,"
                    " schedule_time = :time, run_at = :run_at, timezone = :tz,"
                    " next_run_at = :next WHERE id = cast(:id AS uuid)"
                ),
                {
                    "kind": schedule_kind,
                    "interval": interval_seconds,
                    "freq": schedule_freq,
                    "days": list(schedule_days),
                    "time": schedule_time,
                    "run_at": run_at,
                    "tz": timezone,
                    "next": next_run,
                    "id": schedule_id,
                },
            )
        return (cast(CursorResult[Any], result).rowcount or 0) > 0
