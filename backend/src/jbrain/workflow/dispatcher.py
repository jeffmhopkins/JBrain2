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
from dataclasses import dataclass, field, replace
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
from jbrain.workflow.runlog import EnqueuedStep, PipelineRunLog

log = structlog.get_logger()

# Master on/off gate for the dispatcher tick. Default ON. Since the Wave-2 cutover
# the mode also defaults LIVE, so the engine is the live note->ingest /
# ingest->integrate / resolution->consolidate path (the hardcoded enqueues that
# twinned those events are gone). Flip this to false live (a settings upsert) to
# silence the tick entirely without a redeploy — but with the hardcoded enqueues
# removed, only the recurring reconcilers would then pick up new work, so disabling
# the master switch is an emergency stop, not a no-op. To stop ENQUEUING while still
# diffing, set workflow_dispatch_mode "shadow" instead.
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
    """One job the engine WOULD enqueue for a matched event (shadow: computed, never
    submitted; live: actually enqueued). `kind` is the action's handler key —
    identical to what a hardcoded trigger enqueues — and the stamp is the event's E1
    scope. `trigger_id`/`pipeline` name the trigger + pipeline that produced it, so
    the live path can run-log the dispatch against them (§8)."""

    kind: str
    payload: dict[str, Any]
    principal_id: str
    domain_code: str
    trigger_id: str
    pipeline: str


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
    # The live WouldEnqueue objects this event resolved to — what LIVE mode submits
    # and run-logs. compare=False so it never affects diff equality (the shadow
    # equivalence tests assert on kind/payload via `would`, not these); it is
    # carrier state, not part of the verdict.
    enqueues: list[WouldEnqueue] = field(default_factory=list, compare=False)


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
    *,
    trigger_id: str = "",
) -> tuple[list[WouldEnqueue], list[str]]:
    """The jobs a pipeline's steps WOULD enqueue for an event, plus any resolution
    discrepancies. Each step names a registered action (E3) at the pinned version;
    drift raises DispatchResolutionError. The would-be payload mirrors the hardcoded
    path: the engine carries the event's row-id payload forward, merged over the
    step's static params. The job stamp is the event's (principal_id, domain_code)
    (E1); `trigger_id`/`pipeline` are recorded on each WouldEnqueue so the live path
    can run-log the dispatch."""
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
                trigger_id=trigger_id,
                pipeline=pipeline.name,
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


# The kinds the dispatcher live-enqueues that carry a note-keyed dedup guard,
# mapping each to the queued-only active check the hardcoded path uses so an event
# + the reconciler (or a double-dispatch) never double-process one note (E4). Both
# are queued-only on purpose, mirroring the hardcoded callers: a RUNNING job may
# have read stale chunks, so it must never suppress a fresh enqueue (queue.py
# has_active_analysis docstring; ingest/pipeline.py's queued-only integrate gate).
# A kind absent here has no note-keyed twin; if it carries no per-target payload key
# at all it is deduped kind-only (_KIND_DEDUP below), else its own action keeps its
# dedup.
_NOTE_DEDUP_KINDS: frozenset[str] = frozenset(("ingest_note", "integrate_note"))

# Payload-keyless idempotent sweeps the dispatcher live-enqueues off an event but
# which carry NO per-target key (so the note-keyed guard above cannot apply).
# consolidate_predicates is enqueued off resolution.changed on every remapping
# resolution; without a guard every such event piles up a duplicate (idempotent)
# sweep. Dedup kind-only — suppress a fresh enqueue while one is queued OR running —
# because a duplicate adds no value (the sweep is whole-registry, not per-target) and
# a running sweep already covers any change a re-delivered event reflects.
_KIND_DEDUP_KINDS: frozenset[str] = frozenset(("consolidate_predicates",))


async def _note_state(
    maker: async_sessionmaker[AsyncSession], note_id: str
) -> tuple[str, str] | None:
    """The (ingest_state, integration_state) for a note, or None when it does not
    exist (or is deleted). Read under SYSTEM_CTX — ingest/integration are the owner's
    own cross-domain machinery, exactly like the worker handlers (ingest/pipeline.py).
    A missing/deleted note returns None so the caller treats the would-be enqueue as a
    no-op target the handlers themselves already short-circuit (the handler is a no-op
    on a missing note), never suppressing on absence in a way that could strand work."""
    async with scoped_session(maker, SYSTEM_CTX) as session:
        row = (
            await session.execute(
                text(
                    "SELECT ingest_state, integration_state FROM app.notes"
                    " WHERE id = :nid AND deleted_at IS NULL"
                ),
                {"nid": note_id},
            )
        ).first()
    return (row.ingest_state, row.integration_state) if row is not None else None


async def _already_active(maker: async_sessionmaker[AsyncSession], w: WouldEnqueue) -> bool:
    """Whether this would-be enqueue is redundant — either a QUEUED twin job already
    targets it (a live enqueue would double-process, E4), OR the note is already past
    the STATE the matching reconciler would re-enqueue from (so the dispatcher skips
    exactly what `backfill_pending_notes`/`backfill_pending_integration` would NOT
    re-enqueue, keeping the two safety nets congruent under live).

    Two guards, in this order — the cheap job check first, then the state check:

    - `ingest_note`: skip on a queued `ingest_note` twin (the reconciler's active-job
      check), AND skip when the note's `ingest_state != 'pending'` — once a note is
      `processing`/`indexed`/`failed` the pending reconciler (which keys on
      `ingest_state = 'pending'`) would not re-enqueue it, so neither does a live
      dispatch of a stale/re-delivered `note.created` event.
    - `integrate_note`: skip on a queued integrate twin (the note-keyed
      active-analysis check), AND skip when `integration_state == 'integrated'` — the
      integration reconciler keys on `integration_state <> 'integrated'`, so a note
      already integrated is past it and a re-delivered `note.ingested` is suppressed.

    The job check stays queued-only on purpose (mirroring the hardcoded callers and
    the reconcilers): a RUNNING job may have read stale chunks, so it must never
    suppress a fresh enqueue. The state check closes the OTHER hole the cutover opens —
    a re-delivered/duplicate event for a note whose work already finished (no queued
    twin survives) must not re-process.

    A payload-keyless idempotent sweep (_KIND_DEDUP_KINDS, e.g. consolidate_predicates)
    has no per-target key, so it is deduped kind-only: suppress while a queued OR
    running job of that kind exists. Since W2·C the dispatcher enqueues such a sweep
    off a resolution.changed event on every remapping resolution; without this guard
    each event piled up a duplicate sweep. A kind in neither set is never suppressed
    here; its own action keeps its dedup. All reads run under SYSTEM_CTX (owner-only
    jobs/notes), like the claim loop."""
    note_id = w.payload.get("note_id")
    if w.kind in _KIND_DEDUP_KINDS and not isinstance(note_id, str):
        # A whole-registry sweep with no per-target key: a running sweep already
        # covers any change a re-delivered event reflects, so dedup includes running.
        return await queue.has_active_kind(maker, SYSTEM_CTX, w.kind)
    if w.kind not in _NOTE_DEDUP_KINDS or not isinstance(note_id, str):
        return False
    if w.kind == "integrate_note":
        if await queue.has_active_analysis(maker, SYSTEM_CTX, note_id, statuses=("queued",)):
            return True
        state = await _note_state(maker, note_id)
        # Skip a note already integrated (past the reconciler's eligibility); an
        # absent note (None) is left to the handler's own missing-note no-op.
        return state is not None and state[1] == "integrated"
    # ingest_note: queued twin first, then the pending-state skip.
    if await queue.has_active(
        maker, SYSTEM_CTX, w.kind, payload_field="note_id", value=note_id, statuses=("queued",)
    ):
        return True
    state = await _note_state(maker, note_id)
    # Skip unless the note is still 'pending' (the only state the pending reconciler
    # re-enqueues from); an absent note (None) is the handler's own no-op, not skipped.
    return state is not None and state[0] != "pending"


async def live_enqueue(
    maker: async_sessionmaker[AsyncSession],
    diff: ShadowDiff,
    *,
    run_log: PipelineRunLog,
) -> None:
    """Submit a resolved event's would-be enqueues for real (the Wave-2 LIVE path)
    and run-log the dispatch (§8). For each WouldEnqueue: skip it if an equivalent
    job is already active (the dedup guard, logged, never duplicated), else enqueue
    it with the event's (principal_id, domain_code) stamp (E1) — the SAME stamp a
    hardcoded enqueue would carry once the stamp is wired. Then write ONE pipeline
    `runs` row per (trigger, pipeline) with a `run_step` per enqueued job, so the
    dispatch is diagnosable from the run log alone.

    A diff carrying an error never reaches here (the tick logs it and marks the
    event dispatched without enqueuing); a matchless-but-error-free diff (e.g. an
    unbound event type) has no enqueues and is a no-op. The dedup read + enqueue +
    run-log run AFTER the claim transaction has committed dispatched_at, exactly
    like the scheduler's enqueue runs outside its claim lock."""
    # Group by (trigger_id, pipeline) so one dispatched event that fans into several
    # triggers writes one run per trigger, each owning its steps.
    grouped: dict[tuple[str, str], list[EnqueuedStep]] = {}
    scoped = diff_is_scoped(diff)
    for w in diff.enqueues:
        if await _already_active(maker, w):
            log.info(
                "dispatcher.live_dedup_skip",
                event_id=diff.event_id,
                event_type=diff.event_type,
                kind=w.kind,
                payload=w.payload,
            )
            continue
        # E1: stamp the job with the event's scope when present. A both-empty stamp
        # is a system enqueue (NULL/NULL) the worker runs under SYSTEM_CTX, exactly
        # as a hardcoded enqueue does today; a present stamp narrows at execution.
        stamp = _stamp(w)
        job_id = await queue.enqueue(
            maker,
            SYSTEM_CTX,
            w.kind,
            w.payload,
            principal_id=stamp[0],
            domain_code=stamp[1],
        )
        grouped.setdefault((w.trigger_id, w.pipeline), []).append(
            EnqueuedStep(kind=w.kind, job_id=job_id)
        )
        log.info(
            "dispatcher.live_enqueue",
            event_id=diff.event_id,
            event_type=diff.event_type,
            kind=w.kind,
            job_id=job_id,
            trigger_id=w.trigger_id,
            pipeline=w.pipeline,
        )
    for (trigger_id, pipeline), steps in grouped.items():
        await run_log.record(
            SYSTEM_CTX,
            pipeline=pipeline,
            trigger_id=trigger_id or None,
            ran_as="scoped" if scoped else "system",
            domain_code=diff.enqueues[0].domain_code if scoped else None,
            principal_id=diff.enqueues[0].principal_id if scoped else None,
            steps=steps,
        )


def diff_is_scoped(diff: ShadowDiff) -> bool:
    """Whether this event ran under a NARROWED scope (E1): it carries a triggering
    principal + domain (both halves of the stamp). The worker today emits events
    under the owner principal with the note's domain — a well-formed stamp — so
    these run `scoped`. A would-be enqueue with an empty principal/domain (a system
    event) runs `system` and records that choice on the audit, never a smuggled
    escalation."""
    if not diff.enqueues:
        return False
    w = diff.enqueues[0]
    return bool(w.principal_id) and bool(w.domain_code)


def _stamp(w: WouldEnqueue) -> tuple[str | None, str | None]:
    """The (principal_id, domain_code) to stamp on the enqueued job: the event's
    scope when both halves are present (a narrowed E1 job), else (None, None) — a
    system job the worker runs under SYSTEM_CTX. A half-stamp is never forwarded as
    a partial (which would fail-close in the worker on a value that is really a
    system enqueue); the all-or-nothing decision is made here off both halves."""
    if w.principal_id and w.domain_code:
        return w.principal_id, w.domain_code
    return None, None


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
            would, _ = diff_pipeline(event, pipeline, registry, trigger_id=trigger.trigger_id)
        except (scheduler.ScheduleResolutionError, DispatchResolutionError) as exc:
            return ShadowDiff(
                event_id=event.id, event_type=event.type, matches=False, error=repr(exc)
            )
        all_would.extend(would)
    # `enqueues` carries the live WouldEnqueue objects through to the tick so LIVE
    # mode can submit + run-log them; `compute_diff` owns the shadow verdict.
    return replace(compute_diff(event, all_would), enqueues=all_would)


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
    live: bool = False,
    run_log: PipelineRunLog | None = None,
) -> list[ShadowDiff]:
    """Drain the undispatched-event backlog: resolve each event to its would-be
    enqueue, diff against the hardcoded baseline, log any discrepancy, and stamp
    `dispatched_at`.

    In SHADOW (`live=False`, the default — prod stays here this wave) it NEVER
    enqueues: the hardcoded path still owns the real work, and an enqueue here would
    double-process. In LIVE (`live=True`, the Wave-2 cutover) it ALSO submits each
    resolved would-be enqueue with the event's E1 stamp and run-logs the dispatch
    (live_enqueue), applying the hardcoded path's dedup so an event + the reconciler
    never double-process (E4). The diff still runs in live for observability.

    One claim transaction per event: claim + resolve + mark-dispatched commit
    together so the event leaves the undispatched set atomically. The LIVE enqueue +
    run-log run AFTER that commit (outside the claim lock, like the scheduler), so a
    re-emitted event is never double-claimed; the dedup guard handles the (rare)
    re-enqueue race. Re-querying each pass drains the whole backlog. A
    resolution/authorization error is logged and the event still marked dispatched
    (it must not wedge the loop), never enqueued."""
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
        # LIVE: the claim transaction has committed dispatched_at; now enqueue the
        # resolved jobs (dedup-skipped) + run-log the dispatch. Only a clean,
        # error-free diff enqueues — an error/poison event is logged below and was
        # already marked dispatched, never enqueued (fail-closed, like shadow).
        if live and run_log is not None and diff.error is None and diff.enqueues:
            await live_enqueue(maker, diff, run_log=run_log)
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
# undispatched-event query (an indexed `dispatched_at IS NULL` scan) is polled at.
# Dropped from 15s to 2s for the LIVE path: in shadow the cost of latency was only
# a slightly-staler diff, but once live the tick IS the enqueue, so a freshly
# emitted event must reach the queue within a couple of seconds (the hardcoded path
# enqueued synchronously). The claim query is cheap enough to poll this often, and
# it short-circuits immediately when no event is due (the common idle case).
TICK_SECONDS = 2.0


async def run_tick_safely(
    maker: async_sessionmaker[AsyncSession],
    registry: ActionRegistry,
    *,
    settings: SqlSettingsStore,
    run_log: PipelineRunLog,
) -> None:
    """Run one dispatcher tick, gated by the `workflow_dispatch` master switch + the
    `workflow_dispatch_mode` setting, swallowing failures (mirrors
    scheduler.run_tick_safely): a dispatcher blip must never kill the worker loop.
    Both gates are read live so the operator silences the tick or rolls live→shadow
    without a redeploy. The master switch defaults ON; since the Wave-2 cutover the
    mode defaults LIVE, so the engine is the live enqueue path (the hardcoded enqueues
    are gone). An operator upsert to mode "shadow"/"off" or the master switch to false
    rolls it back. `off` (either gate) skips the tick entirely."""
    try:
        enabled = await settings.get(SYSTEM_CTX, WORKFLOW_DISPATCH_KEY, WORKFLOW_DISPATCH_DEFAULT)
        if enabled is not True:
            return
        mode = await settings.workflow_dispatch_mode(SYSTEM_CTX)
        if mode == "off":
            return
        await dispatcher_tick(maker, registry, live=mode == "live", run_log=run_log)
    except Exception as exc:  # noqa: BLE001 - the tick must not crash the worker
        log.warning("dispatcher.tick_error", error=repr(exc))
