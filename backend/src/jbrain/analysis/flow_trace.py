"""Toggleable per-note pipeline flow trace (config `analysis_trace`).

Distinct from `analysis.trace`, which builds the persisted review-card explanation
for a single HELD fact. This one is live operator lighting: when on, each seam of
`integrate_note` emits ONE structured INFO event keyed by note_id — vision (the
OCR/caption an attachment produced) → extract → integrate → recover → plan →
per-fact commit decision — so an operator tailing the worker logs can watch a
single note's facts flow end to end, from what the image model read to where an
edge is dropped, refreshed, or superseded.

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
    """Whether per-note flow tracing is on. Cached after the first read: both
    flags are env-fixed for the process, so this avoids re-parsing settings per
    fact.

    Auto-arms whenever the debug console is enabled: that gate is the only way
    these logs get read and is the prerequisite for minting an assistant's debug
    token, so an enabled console IS the debugging session this trace exists for.
    The explicit `analysis_trace` flag stays as an override for tracing without
    the console."""
    global _enabled
    if _enabled is None:
        s = get_settings()
        _enabled = s.analysis_trace or s.debug_access_enabled
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


# The vision text can be long (a full screenshot transcription); cap it so a
# trace line stays readable while still showing what the image model actually read.
_VISION_TEXT_CAP = 2000


def _short(value: Any) -> str | None:
    """First segment of a UUID/id — compact and greppable in log lines."""
    if value is None:
        return None
    return str(value).split("-", 1)[0]


def vision(
    attachment_id: Any,
    *,
    note_id: Any,
    kind: str,
    provider: str,
    model: str,
    filename: str,
    text: str,
) -> None:
    """Seam 0 — what vision.ocr / vision.caption produced for an attachment, BEFORE
    it becomes chunks the extractor mines. Logs the verbatim transcription/caption
    (capped) with the model that produced it, so an operator can see exactly what
    the image model read — where a screenshot's app chrome or a tool's own narration
    entered the pipeline — without querying the extract cache. Same debug-console
    gate as the rest of the trace."""
    if not enabled():
        return
    log.info(
        "analysis.flow.vision",
        attachment_id=_short(attachment_id),
        note_id=_short(note_id),
        kind=kind,
        provider=provider,
        model=model,
        filename=filename,
        chars=len(text),
        text=text[:_VISION_TEXT_CAP],
        truncated=len(text) > _VISION_TEXT_CAP,
    )


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


def _fact_brief(f: Any) -> dict[str, Any]:
    """One intent fact, verbose enough to see what the integrator emitted — the
    value and the resolved temporal in particular, since a new-entity pass can drop
    either and leave a backstop nothing to ground."""
    brief: dict[str, Any] = {
        "edge": _edge(f.entity_ref, f.predicate, f.qualifier, f.object_entity_ref),
        "kind": f.kind,
        "assertion": f.assertion,
        "inferred": f.inferred,
    }
    if isinstance(f.value_json, dict) and f.value_json:
        brief["value"] = f.value_json.get("value", f.value_json)
    if f.temporal is not None:
        brief["temporal"] = {
            "phrase": f.temporal.phrase,
            "start": str(f.temporal.resolved_start) if f.temporal.resolved_start else None,
        }
    return brief


def intent(note_id: str, stage: str, value: IntegrationIntent) -> None:
    """Seams 2 & 3 — the integrator's intent (stage="integrate") and the same
    intent after object-ref recovery + gender derivation (stage="recover"). Logs
    EVERY fact (not just edges) with its value, temporal, and inferred flag, so a
    field the integrator dropped — and whether recover/derive restored it — is
    visible side by side across the two stages."""
    if not enabled():
        return
    log.info(
        "analysis.flow.intent",
        note_id=note_id,
        stage=stage,
        resolutions=[f"{r.mention_ref}:{r.mode}" for r in value.entity_resolutions],
        facts=[_fact_brief(f) for f in value.facts],
        supersessions=[
            f"{s.entity_ref}.{s.predicate}:{s.action}" for s in value.supersession_proposals
        ],
    )


def _planned_brief(pf: Any, sig: Any) -> dict[str, Any]:
    """One planned fact with the deterministic signals behind its weight — exactly
    why it commits or holds: surface_attested (any grounding fired), inferred, and
    whether the fields a grounding backstop needs (value / temporal / span) are
    present. A held attribute with surface_attested=False + has_temporal=False is a
    date the integrator emitted without its phrase, etc."""
    f = pf.fact
    brief: dict[str, Any] = {
        "edge": _edge(f.entity_ref, f.predicate, f.qualifier, f.object_entity_ref),
        "kind": f.kind,
        "status": pf.status,
        "weight": round(pf.weight, 3),
        "review": list(pf.review_reasons),
        "inferred": f.inferred,
        "self_conf": round(f.self_confidence, 2),
        "has_value": isinstance(f.value_json, dict) and bool(f.value_json),
        "has_temporal": f.temporal is not None and f.temporal.resolved_start is not None,
        "has_span": f.attested_span is not None,
    }
    if sig is not None:
        brief["surface_attested"] = sig.surface_attested
    return brief


def plan(note_id: str, value: ArbiterPlan, signals: Any = None) -> None:
    """Seam 4 — the deterministic disposition: which facts commit vs. route to
    review, with the per-fact signals (surface_attested etc.) that produced the
    weight, so an under-attested fact's MISSING grounding input is visible."""
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
    sig = signals or {}
    log.info(
        "analysis.flow.plan",
        note_id=note_id,
        facts=[_planned_brief(pf, sig.get(i)) for i, pf in enumerate(value.facts)],
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
