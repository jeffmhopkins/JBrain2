"""Typed workflow-engine shapes the Wave-1 tracks build against.

Defined once in Wave 0 (docs/archive/WORKFLOW_ENGINE_PLAN.md §5 W0.2) so the dispatcher
(Track A), the scheduler (Track B), the eval harness (Track C), and the run-log
UI (Track D) agree on a fixed surface before any of them is written: an event,
the filter a trigger matches it against, an ordered pipeline step, and a schedule
tick. These are the *definition/dispatch* shapes — reference, not the executor:
an action names an existing registered handler (E3), and the action registry that
validates handler existence at boot is the sibling W0.1 task's `registry.py`, not
here.

Serializable Pydantic models so a trigger filter, a pipeline definition, and a
schedule round-trip cleanly through the `jsonb` columns migration 0036 creates.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# An event's fail-closed domain stamp (E2): the most-restrictive scope the
# triggering content touched. Reuses the four firewall domains; a trigger may
# not fan an event into a pipeline that writes a different domain.
Domain = Literal["general", "health", "finance", "location"]

# The unification target for `runs.kind` (§3): the agent loop, the Integrator
# turn-loop, and a data-defined pipeline all log to the one `runs` table.
RunKind = Literal["agent", "integration", "pipeline"]

# E1's recorded scope choice: an owner/agent trigger narrows to the trigger's
# scope; a system/scheduled or legitimately-cross-domain pipeline keeps SYSTEM_CTX
# but records *that it did* on the run (owner-system, not a smuggled escalation).
RanAs = Literal["scoped", "system"]

# A pin memoizes one stochastic Integrator decision against the text it was made
# about (analysis/pins.py): which entity a mention is, or the canonical predicate
# for a key. Mirrors `analysis.pins.DecisionKind` — kept in lockstep so the
# persisted `resolution_pin` key matches the pure shape exactly.
DecisionKind = Literal["identity", "predicate_key"]


class Event(BaseModel):
    """An append-only event-log row (the `events` table): something happened that
    a trigger may bind to a pipeline. `domain` is the fail-closed stamp (E2);
    `principal_id` is the triggering identity the dispatcher narrows a scope from
    (E1). `dispatched_at` is None until the dispatcher has fanned it out."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    domain: Domain
    principal_id: str
    occurred_at: str
    dispatched_at: str | None = None


class TriggerFilter(BaseModel):
    """The `filter` a trigger matches a candidate event against before firing its
    pipeline. Conjunctive and data-only (no code): an event fires the trigger when
    its `type` is in `event_types` (empty = any) and every `payload_equals` entry
    matches the event payload. Kept deliberately small — richer predicates are a
    §7 open decision, not Wave 0."""

    model_config = ConfigDict(extra="forbid")

    event_types: list[str] = Field(default_factory=list)
    # Domains the trigger accepts; empty = any. A trigger may never widen an
    # event into a pipeline that writes a different domain (E2) — this is the
    # accept-side check, enforced by the dispatcher, not a widening.
    domains: list[Domain] = Field(default_factory=list)
    payload_equals: dict[str, Any] = Field(default_factory=dict)
    # Which event-payload keys cross into the enqueued job payload. Empty = the
    # dispatcher default ({"note_id"}), preserving the note pipelines. A trigger
    # for a different event (e.g. location.geofence_transition) declares exactly
    # the opaque ids it forwards — and deliberately NOT raw coordinates. Per
    # trigger, so widening one event's forward set never leaks keys into another.
    forward_keys: list[str] = Field(default_factory=list)


class PipelineStep(BaseModel):
    """One ordered step of a pipeline: a reference to a registered action by
    name+version (E3, never inline code) plus the static params bound at
    definition time. Pipelines are linear first; a DAG is deferred (§7), so a step
    has no explicit predecessors — order in the list is the order of execution."""

    model_config = ConfigDict(extra="forbid")

    action: str
    action_version: int = Field(ge=1)
    params: dict[str, Any] = Field(default_factory=dict)


class Pipeline(BaseModel):
    """A stored pipeline definition (the `pipelines` table `steps` jsonb): an
    ordered list of action refs. Ingest and integration each become one of these
    in Wave 2 (E7)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = Field(ge=1)
    steps: list[PipelineStep]
    description: str = ""


class Schedule(BaseModel):
    """A scheduler claim target (the `schedules` table): an interval + an explicit
    `next_run_at` the tick advances app-side, so a fake clock controls it in tests
    (N3) and no cron-parser dependency is added (§7, zero-new-dep goal).
    `interval_seconds` is the recurrence; nightly is 86400 at owner-local 02:00."""

    model_config = ConfigDict(extra="forbid")

    id: str
    interval_seconds: int = Field(gt=0)
    timezone: str
    next_run_at: str
    last_run_at: str | None = None
    enabled: bool = True


# A trigger binds either an event type or a schedule to a pipeline; exactly one
# of the two source forms is set. Modeled as a discriminated union so Track A and
# Track B switch on `source` without guessing.


class EventTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["event"] = "event"
    id: str
    pipeline: str
    filter: TriggerFilter = Field(default_factory=TriggerFilter)
    enabled: bool = True
    # An emergency-fireable sweep surfaces a manual "run now" control in Ops.
    manual: bool = False


class ScheduleTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["schedule"] = "schedule"
    id: str
    schedule_id: str
    pipeline: str
    enabled: bool = True
    manual: bool = False


Trigger = Annotated[EventTrigger | ScheduleTrigger, Field(discriminator="source")]
