"""Toggleable per-note pipeline flow trace (config `analysis_trace`).

Distinct from `analysis.trace`, which builds the persisted review-card explanation
for a single HELD fact. This one is live operator lighting: when on, each seam of
`integrate_note` emits ONE structured INFO event keyed by note_id — extract →
integrate → recover → plan → per-fact commit decision — so an operator tailing the
worker logs can watch a single note's facts flow end to end and see exactly where
an edge is dropped, refreshed, or superseded.

OFF by default and cheap when off: every emitter checks one cached flag before
touching its arguments, so the hot path pays nothing in production. This module is
pure observability — it reads pipeline state and never changes a disposition.

The flag is read once per process (the env value is fixed for a worker's life), so
an operator turns tracing on by setting JBRAIN_ANALYSIS_TRACE=true and restarting
the worker. Tests flip it with `set_enabled` / `reset`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from jbrain.config import get_settings

if TYPE_CHECKING:
    from jbrain.analysis.arbiter import ArbiterPlan
    from jbrain.analysis.extraction import Extraction
    from jbrain.analysis.intent import IntegrationIntent
    from jbrain.analysis.supersession import Decision, FactView

log = structlog.get_logger()

_enabled: bool | None = None


def enabled() -> bool:
    """Whether per-note flow tracing is on. Cached after the first read: the env
    flag is fixed for the process, so this avoids re-parsing settings per fact."""
    global _enabled
    if _enabled is None:
        _enabled = get_settings().analysis_trace
    return _enabled


def reset() -> None:
    """Drop the cached flag so the next `enabled()` re-reads the environment
    (tests only; production reads once and never flips mid-process)."""
    global _enabled
    _enabled = None


def set_enabled(value: bool) -> None:
    """Force the cached flag without touching the environment (tests only)."""
    global _enabled
    _enabled = value


def _short(value: Any) -> str | None:
    """First segment of a UUID/id — compact and greppable in log lines."""
    if value is None:
        return None
    return str(value).split("-", 1)[0]


def _edge(entity_ref: str, predicate: str, qualifier: str, obj: str | None) -> str:
    qual = f".{qualifier}" if qualifier else ""
    arrow = f" -> {obj}" if obj else ""
    return f"{entity_ref}.{predicate}{qual}{arrow}"


def extract(note_id: str, extraction: Extraction) -> None:
    """Seam 1 — what the note.extract call surfaced, before any judgment."""
    if not enabled():
        return
    log.info(
        "analysis.flow.extract",
        note_id=note_id,
        mentions=[m.name for m in extraction.mentions],
        facts=len(extraction.facts),
        edges=[
            _edge(f.entity_ref, f.predicate, f.qualifier, f.object_entity_ref)
            for f in extraction.facts
            if f.kind == "relationship"
        ],
    )


def intent(note_id: str, stage: str, value: IntegrationIntent) -> None:
    """Seams 2 & 3 — the integrator's intent (stage="integrate") and the same
    intent after object-ref recovery (stage="recover"). Emitting both shows a
    field the integrator dropped and the recovery restored, side by side."""
    if not enabled():
        return
    log.info(
        "analysis.flow.intent",
        note_id=note_id,
        stage=stage,
        resolutions=[f"{r.mention_ref}:{r.mode}" for r in value.entity_resolutions],
        edges=[
            _edge(f.entity_ref, f.predicate, f.qualifier, f.object_entity_ref)
            for f in value.facts
            if f.kind == "relationship"
        ],
        supersessions=[
            f"{s.entity_ref}.{s.predicate}:{s.action}" for s in value.supersession_proposals
        ],
    )


def plan(note_id: str, value: ArbiterPlan) -> None:
    """Seam 4 — the deterministic disposition: which facts commit vs. route to
    review, with weight and the reason a fact was forced to review."""
    if not enabled():
        return
    if value.rejected:
        log.info(
            "analysis.flow.plan",
            note_id=note_id,
            rejected=True,
            violations=[v.code for v in value.fatal_violations],
        )
        return
    log.info(
        "analysis.flow.plan",
        note_id=note_id,
        facts=[
            {
                "edge": _edge(
                    pf.fact.entity_ref,
                    pf.fact.predicate,
                    pf.fact.qualifier,
                    pf.fact.object_entity_ref,
                ),
                "status": pf.status,
                "weight": round(pf.weight, 3),
                "review": list(pf.review_reasons),
            }
            for pf in value.facts
        ],
    )


def _verb(decision: Decision) -> str:
    if decision.refresh_id is not None:
        return "refresh"
    if decision.close_id is not None:
        return "close"
    if decision.insert and decision.supersede_ids:
        return "insert+supersede"
    if decision.insert:
        return "insert"
    return "noop"


def commit(
    note_id: str,
    *,
    entity_ref: str,
    predicate: str,
    qualifier: str,
    object_ref: str | None,
    subject_id: Any,
    object_id: Any,
    existing: list[FactView],
    decision: Decision,
) -> None:
    """Seam 5 — the per-fact commit decision against the resolved graph. `existing`
    is exactly what the identity-key lookup returned, so an enumerated edge that
    pulls back a sibling's row (or resolves to refresh/supersede instead of insert)
    is the collapse made visible at the moment it happens."""
    if not enabled():
        return
    log.info(
        "analysis.flow.commit",
        note_id=note_id,
        edge=_edge(entity_ref, predicate, qualifier, object_ref),
        subject_id=_short(subject_id),
        object_id=_short(object_id),
        verb=_verb(decision),
        existing=[
            {"id": _short(e.id), "obj": _short(e.object_entity_id), "status": e.status}
            for e in existing
        ],
        supersedes=len(decision.supersede_ids),
        holds=len(decision.hold_ids),
        insert_status=decision.insert_status if decision.insert else None,
    )
