"""The event->trigger->pipeline->action dispatcher, in SHADOW mode (Track A, E7a).

The engine's critical-path spine: claim undispatched `app.events`, resolve each
event's `type` to the enabled `on_event` trigger(s) it matches, resolve those
triggers' pipelines to their action steps, and compute the jobs the engine WOULD
enqueue. In Wave 1 it does NOT enqueue — the hardcoded trigger points still own
the real path (note->ingest, ingest->integrate, resolution->consolidate), and a
real enqueue here would DOUBLE-process every note. Instead it DIFFS its would-be
enqueue against what the hardcoded path actually enqueued (recorded on the event's
`_shadow_enqueued` payload, workflow/events.py) and logs any discrepancy
(`dispatcher.shadow_diff`). Wave 2 flips the `workflow_dispatch` setting from
shadow to live and removes the hardcoded enqueues once the diff is clean.

Resolution reuses Track B's helpers (`scheduler._load_pipeline`,
`registry.get`) — the engine has ONE resolution path, time-driven and
event-driven alike; this track does not duplicate it.

Three security properties hold even in shadow (they are the whole point of
shadowing before cutover):

- **E3, registry actions only.** A pipeline step names a registered action or the
  resolution fails loudly; the engine can never invent a handler.
- **E2, fail-closed domain.** A trigger may not fan an event into a pipeline that
  writes a different domain than the event's stamp; the trigger filter's `domains`
  is an accept-side check, never a widening.
- **E1, domain authorization (the check A3 deferred to this layer).** A would-be
  job carries the event's (principal_id, domain_code) stamp; before "enqueuing" it
  the dispatcher validates the principal is entitled to that domain by building the
  narrowed `SessionContext` (db.session.narrowed_context). A malformed stamp or an
  unentitled domain is a fail-closed SHADOW ERROR — logged, the event still marked
  dispatched, never an enqueue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.db.session import ScopeStampError, narrowed_context, scoped_session
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow import events as event_emit
from jbrain.workflow import scheduler
from jbrain.workflow.contracts import Pipeline, TriggerFilter
from jbrain.workflow.registry import ActionRegistry, ActionRegistryError

log = structlog.get_logger()

# Gate for the dispatcher tick. Default ON for SHADOW: the dispatcher runs and
# diffs from the first boot of this wave, but a real enqueue stays off until the
# Wave-2 cutover (which is a code change, not just a flag flip). Flip to false
# live (a settings upsert) to silence the shadow tick without a redeploy.
WORKFLOW_DISPATCH_KEY = "workflow_dispatch"
WORKFLOW_DISPATCH_DEFAULT = True

# A claimed event is dispatched under the system context: reading the trigger/
# pipeline definitions (owner/reference data) and stamping dispatched_at on the
# event row are owner-system operations, exactly like the scheduler tick. The
# PER-EVENT narrowing (E1) happens against the event's own stamp, not here.
SYSTEM_CTX = queue.SYSTEM_CTX

VALID_DOMAINS: frozenset[str] = frozenset(("general", "health", "finance", "location"))

# The event-payload keys that cross into a would-be job's payload (the row ids a
# pipeline action consumes). The note pipelines forward `note_id`; the resolution
# pipeline forwards nothing (consolidate takes an empty payload), so the event's
# `item_id` metadata is deliberately NOT here. Every other event-payload key
# (incl. the `_shadow_enqueued` diff baseline) is metadata, never a job param.
FORWARD_KEYS: frozenset[str] = frozenset(("note_id",))


class DispatchResolutionError(Exception):
    """An event matched a trigger whose pipeline does not exist or references an
    unregistered action — config drift surfaced (not swallowed) so a misconfigured
    trigger is diagnosable rather than a silently dropped event."""


@dataclass(frozen=True)
class WouldEnqueue:
    """One job the engine WOULD enqueue for a matched event in live mode (shadow:
    computed, never submitted). `kind` is the action's handler key — identical to
    what a hardcoded trigger enqueues — and the stamp is the event's E1 scope."""

    kind: str
    payload: dict[str, Any]
    principal_id: str
    domain_code: str


@dataclass(frozen=True)
class ShadowDiff:
    """The shadow-equivalence verdict for one event (E7a). `would` is what the
    engine computed; `actual` is the hardcoded path's recorded `_shadow_enqueued`.
    `matches` is the diff result the dispatcher logs; `discrepancies` explains a
    mismatch. A fail-closed authorization/resolution error sets `error`."""

    event_id: str
    event_type: str
    matches: bool
    would: list[dict[str, Any]] = field(default_factory=list)
    actual: dict[str, Any] | None = None
    discrepancies: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class _CandidateEvent:
    """A claimed undispatched event, as the resolver consumes it."""

    id: str
    type: str
    payload: dict[str, Any]
    domain_code: str
    principal_id: str


@dataclass(frozen=True)
class _MatchedTrigger:
    """An enabled on_event trigger whose filter accepted a candidate event."""

    trigger_id: str
    pipeline: str
    filter: TriggerFilter


def event_matches(filter_: TriggerFilter, event_type: str, payload: dict[str, Any]) -> bool:
    """Whether a trigger's conjunctive filter accepts this event (contracts.py).

    `event_types` empty = any type; otherwise the type must be listed. Every
    `payload_equals` entry must match the event payload. `domains` is checked
    SEPARATELY against the event's domain stamp (a domain mismatch is an E2
    fail-closed condition the dispatcher rejects, not a quiet non-match here)."""
    if filter_.event_types and event_type not in filter_.event_types:
        return False
    return all(payload.get(k) == v for k, v in filter_.payload_equals.items())


def authorize_domain(principal_id: str, domain_code: str) -> str | None:
    """The E1 domain-authorization check A3 deferred to this layer: validate the
    triggering principal is entitled to the event's domain before stamping a
    would-be job with it.

    Returns None when authorized, or a fail-closed reason string. The check has two
    teeth: the domain must be a real firewall domain (an unknown code can never be
    entitled), and the (principal_id, domain_code) stamp must build a narrowed
    `SessionContext` without raising — a malformed/partial stamp is a confused-deputy
    smell that fails CLOSED (db.session.narrowed_context), never a silent widening to
    the all-domains scope."""
    if domain_code not in VALID_DOMAINS:
        return f"unknown domain {domain_code!r}"
    try:
        # Building the narrowed context is the authorization: it is the exact scope
        # a live enqueue would carry, and it raises on a malformed stamp. The single
        # owner is entitled to every real domain, so a well-formed stamp authorizes;
        # the narrowing is what a non-owner (Phase 7) scoped principal would be
        # firewalled by. We construct it to PROVE it is constructible, fail-closed.
        narrowed_context(principal_id, domain_code)
    except ScopeStampError as exc:
        return f"malformed scope stamp: {exc}"
    return None


def diff_pipeline(
    event: _CandidateEvent,
    pipeline: Pipeline,
    registry: ActionRegistry,
) -> tuple[list[WouldEnqueue], list[str]]:
    """The jobs a pipeline's steps WOULD enqueue for an event, plus any resolution
    discrepancies. Each step names a registered action (E3) at the pinned version;
    drift raises DispatchResolutionError. The would-be payload mirrors the hardcoded
    path: the engine carries the event's row-id payload forward, merged over the
    step's static params. The job stamp is the event's (principal_id, domain_code)
    (E1)."""
    would: list[WouldEnqueue] = []
    for step in pipeline.steps:
        try:
            spec = registry.get(step.action)
        except ActionRegistryError as exc:
            raise DispatchResolutionError(
                f"pipeline {pipeline.name!r} references {step.action!r}: {exc}"
            ) from exc
        if spec.version != step.action_version:
            raise DispatchResolutionError(
                f"pipeline {pipeline.name!r} pins action {step.action!r}"
                f" v{step.action_version}, registry has v{spec.version}"
            )
        # The would-be job payload reproduces the hardcoded enqueue: the note
        # pipelines forward the {note_id} row id; the resolution pipeline forwards
        # nothing (consolidate enqueues {}). Only FORWARD_KEYS cross from the event
        # into the job — the event's own metadata (item_id, the _shadow_enqueued
        # baseline) is never a job param. Step static params overlay the forward.
        forwarded = {k: event.payload[k] for k in FORWARD_KEYS if k in event.payload}
        merged = {**forwarded, **step.params}
        would.append(
            WouldEnqueue(
                kind=spec.handler,
                payload=merged,
                principal_id=event.principal_id,
                domain_code=event.domain_code,
            )
        )
    return would, []


def compute_diff(
    event: _CandidateEvent,
    would: list[WouldEnqueue],
) -> ShadowDiff:
    """Compare the engine's would-be enqueues against the hardcoded path's recorded
    baseline (`_shadow_enqueued`, E7a). Shadow equivalence is: the engine produces
    exactly the job kind the hardcoded path produced. A one-action pipeline (the
    three seeded defs) should yield exactly one would-be enqueue whose `kind`
    matches the baseline `kind`; the payload should carry the same row id.

    A missing baseline (an event with no `_shadow_enqueued`) is not a mismatch — it
    is an unobservable event (e.g. a future event type with no hardcoded twin); the
    diff records `actual=None` and treats kind-only-known as informational."""
    actual = event.payload.get(event_emit.SHADOW_ENQUEUED_KEY)
    discrepancies: list[str] = []
    if actual is None:
        # No baseline to diff against — the engine's would-be enqueue is recorded
        # but not asserted equivalent (nothing to compare to).
        return ShadowDiff(
            event_id=event.id,
            event_type=event.type,
            matches=True,
            would=[_describe(w) for w in would],
            actual=None,
        )
    actual_kind = actual.get("kind")
    actual_payload = actual.get("payload") or {}
    would_kinds = [w.kind for w in would]
    if would_kinds != [actual_kind]:
        discrepancies.append(
            f"kind mismatch: engine would enqueue {would_kinds}, hardcoded enqueued {actual_kind!r}"
        )
    elif would and would[0].payload != actual_payload:
        discrepancies.append(
            f"payload mismatch: engine {would[0].payload}, hardcoded {actual_payload}"
        )
    return ShadowDiff(
        event_id=event.id,
        event_type=event.type,
        matches=not discrepancies,
        would=[_describe(w) for w in would],
        actual=actual,
        discrepancies=discrepancies,
    )


def _describe(w: WouldEnqueue) -> dict[str, Any]:
    return {"kind": w.kind, "payload": w.payload, "domain_code": w.domain_code}


async def _matching_triggers(
    session: AsyncSession, event: _CandidateEvent
) -> list[_MatchedTrigger]:
    """Enabled `on_event` triggers whose filter accepts this event. The DB query
    narrows by event type (the indexed `triggers_event_idx` partial index); the
    in-code `event_matches` applies the rest of the conjunctive filter. A trigger
    with no on_event (schedule-bound) is excluded by the `on_event = :type` clause."""
    rows = (
        await session.execute(
            text(
                "SELECT id::text AS id, pipeline, filter::text AS filter"
                " FROM app.triggers"
                " WHERE enabled AND on_event = :type"
            ),
            {"type": event.type},
        )
    ).all()
    matched: list[_MatchedTrigger] = []
    for row in rows:
        filter_ = TriggerFilter.model_validate_json(row.filter)
        if event_matches(filter_, event.type, event.payload):
            matched.append(
                _MatchedTrigger(trigger_id=row.id, pipeline=row.pipeline, filter=filter_)
            )
    return matched


def _accepts_domain(filter_: TriggerFilter, domain_code: str) -> bool:
    """The E2 accept-side check: a trigger whose filter pins `domains` may only fan
    an event whose domain is in that set. Empty = any (the seed triggers accept any
    domain — the pipeline action itself is cross-domain). This is an ACCEPT gate,
    never a widening: a trigger can refuse a domain, never grant one."""
    return not filter_.domains or domain_code in filter_.domains


async def resolve_event(
    session: AsyncSession,
    registry: ActionRegistry,
    event: _CandidateEvent,
) -> ShadowDiff:
    """Resolve one claimed event to its shadow-diff verdict (no enqueue, no commit).

    The full chain: match enabled on_event triggers -> E2 accept-side domain check
    -> E1 domain authorization -> resolve each pipeline's actions (E3) to would-be
    enqueues -> diff against the recorded hardcoded baseline. Any fail-closed
    condition (unentitled domain, malformed stamp, unresolvable pipeline) returns a
    diff carrying `error`; the caller still marks the event dispatched (a poison
    event must not wedge the claim loop) but never enqueues."""
    # E1 authorization first: an event whose stamp is unentitled/malformed never
    # reaches a pipeline, fail-closed.
    auth_error = authorize_domain(event.principal_id, event.domain_code)
    if auth_error is not None:
        return ShadowDiff(event_id=event.id, event_type=event.type, matches=False, error=auth_error)
    triggers = await _matching_triggers(session, event)
    all_would: list[WouldEnqueue] = []
    for trigger in triggers:
        if not _accepts_domain(trigger.filter, event.domain_code):
            return ShadowDiff(
                event_id=event.id,
                event_type=event.type,
                matches=False,
                error=(
                    f"trigger {trigger.trigger_id} does not accept domain"
                    f" {event.domain_code!r} (E2 fail-closed)"
                ),
            )
        try:
            pipeline = await scheduler._load_pipeline(session, trigger.pipeline)
            would, _ = diff_pipeline(event, pipeline, registry)
        except (scheduler.ScheduleResolutionError, DispatchResolutionError) as exc:
            return ShadowDiff(
                event_id=event.id, event_type=event.type, matches=False, error=repr(exc)
            )
        all_would.extend(would)
    return compute_diff(event, all_would)


async def _claim_event(session: AsyncSession) -> _CandidateEvent | None:
    """Claim the oldest undispatched event FOR UPDATE SKIP LOCKED (so a second
    worker never double-dispatches one event, §7). Returns None when none is due."""
    row = (
        await session.execute(
            text(
                "SELECT id::text AS id, type, payload::text AS payload,"
                "       domain_code, principal_id::text AS principal_id"
                " FROM app.events"
                " WHERE dispatched_at IS NULL"
                " ORDER BY occurred_at"
                " FOR UPDATE SKIP LOCKED"
                " LIMIT 1"
            )
        )
    ).first()
    if row is None:
        return None
    return _CandidateEvent(
        id=row.id,
        type=row.type,
        payload=json.loads(row.payload),
        domain_code=row.domain_code,
        principal_id=row.principal_id,
    )


async def dispatcher_tick(
    maker: async_sessionmaker[AsyncSession],
    registry: ActionRegistry,
    *,
    now: datetime | None = None,
) -> list[ShadowDiff]:
    """Drain the undispatched-event backlog in SHADOW mode: resolve each event to
    its would-be enqueue, diff against the hardcoded baseline, log any discrepancy,
    and stamp `dispatched_at` — WITHOUT enqueuing (the hardcoded path still owns the
    real work this wave; an enqueue here would double-process).

    One claim transaction per event: claim + resolve + mark-dispatched commit
    together so the event leaves the undispatched set atomically. Re-querying each
    pass drains the whole backlog. A resolution/authorization error is logged and
    the event still marked dispatched (it must not wedge the loop), never enqueued."""
    diffs: list[ShadowDiff] = []
    while True:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            event = await _claim_event(session)
            if event is None:
                return diffs
            diff = await resolve_event(session, registry, event)
            stamped = now.isoformat() if now is not None else None
            await session.execute(
                text(
                    "UPDATE app.events"
                    " SET dispatched_at = coalesce(cast(:stamped AS timestamptz), now())"
                    " WHERE id = :id"
                ),
                {"id": event.id, "stamped": stamped},
            )
        diffs.append(diff)
        if diff.error is not None:
            # A fail-closed shadow error (E1/E2/E3): the event is marked dispatched
            # but no job would be enqueued. Loud, never silent.
            log.error(
                "dispatcher.shadow_error",
                event_id=diff.event_id,
                event_type=diff.event_type,
                error=diff.error,
            )
        elif not diff.matches:
            log.warning(
                "dispatcher.shadow_diff",
                event_id=diff.event_id,
                event_type=diff.event_type,
                would=diff.would,
                actual=diff.actual,
                discrepancies=diff.discrepancies,
            )
        else:
            log.info(
                "dispatcher.shadow_match",
                event_id=diff.event_id,
                event_type=diff.event_type,
                would=diff.would,
            )


# How often the worker loop runs the dispatcher tick — the cadence the cheap
# undispatched-event query is polled at. Kept low so a freshly emitted event is
# diffed within seconds (the shadow signal is most useful while the hardcoded path
# is still warm).
TICK_SECONDS = 15.0


async def run_tick_safely(
    maker: async_sessionmaker[AsyncSession],
    registry: ActionRegistry,
    *,
    settings: SqlSettingsStore,
) -> None:
    """Run one dispatcher tick, gated by the `workflow_dispatch` setting and
    swallowing failures (mirrors scheduler.run_tick_safely): a dispatcher blip must
    never kill the worker loop. The gate is read live so the operator can silence
    the shadow tick without a redeploy; default ON for shadow."""
    try:
        enabled = await settings.get(SYSTEM_CTX, WORKFLOW_DISPATCH_KEY, WORKFLOW_DISPATCH_DEFAULT)
        if enabled is not True:
            return
        await dispatcher_tick(maker, registry)
    except Exception as exc:  # noqa: BLE001 - the tick must not crash the worker
        log.warning("dispatcher.tick_error", error=repr(exc))
