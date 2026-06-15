"""The review-card process trace — a verbose, per-stage record of HOW a held
fact reached the review inbox (extraction -> integration -> arbiter).

A low_confidence_inference card already shows WHAT it proposes (the edge and the
value). This builds the optional WHY: the same three pipeline stages the review
UI plays back, so a held fact can be debugged from the card alone instead of
re-reading logs. It is a pure projection of objects the arbiter already holds at
card-filing time (the IntentFact, the deterministic ConfidenceSignals, the
PlannedFact verdict) — it stores nothing the pipeline didn't already compute, so
it can never disagree with the decision it explains.

Shape (persisted verbatim into ReviewItem.payload["trace"], rendered by the
frontend timeline + console): `{"stages": [{key, name, version, summary, rows}]}`
where each `row` is a `[label, value]` pair. Deliberately string-only and
display-shaped so the renderer needs no stage-specific knowledge.
"""

from __future__ import annotations

from typing import Any

from jbrain.analysis.arbiter import PlannedFact
from jbrain.analysis.intent import EntityResolution, IntentFact
from jbrain.analysis.weight import (
    COMMIT_THRESHOLDS,
    DEFAULT_THRESHOLD,
    ConfidenceSignals,
    ceiling,
)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _edge(fact: IntentFact) -> str:
    return f"{fact.predicate}.{fact.qualifier}" if fact.qualifier else fact.predicate


def _value(fact: IntentFact) -> str:
    """The bare datum the card writes, falling back to the statement when the
    value lives only in prose (a null value_json is itself the v5 regression the
    card surfaces, so it is worth showing here too)."""
    if isinstance(fact.value_json, dict):
        datum = fact.value_json.get("value")
        if isinstance(datum, str) and datum.strip():
            return datum.strip()
    return fact.statement


def _resolution(entity_ref: str, resolution: EntityResolution | None) -> str:
    if resolution is None:
        return f"{entity_ref} → unresolved"
    if resolution.mode == "existing":
        return f"{entity_ref} → existing entity"
    if resolution.mode == "new":
        return f"{entity_ref} → new {resolution.new_kind or 'entity'}"
    return f"{entity_ref} → ambiguous (held)"


def build_trace(
    fact: IntentFact,
    planned: PlannedFact,
    signals: ConfidenceSignals,
    *,
    resolution: EntityResolution | None,
    supersession_action: str | None,
    extract_version: str,
    integrate_version: str,
    integrator_version: str,
) -> dict[str, Any]:
    """Project one held fact's three pipeline stages into the display trace.

    `signals`/`planned` are the SAME objects the arbiter used to hold this fact,
    so the arbiter stage shows the actual ceiling arithmetic behind the verdict,
    not a re-derivation that could drift from it."""
    cap = ceiling(signals)
    threshold = COMMIT_THRESHOLDS.get(fact.kind, DEFAULT_THRESHOLD)
    value = _value(fact)
    mode = resolution.mode if resolution is not None else "unresolved"

    extraction = {
        "key": "extraction",
        "name": "Extraction",
        "version": extract_version,
        "summary": f'candidate {fact.kind} · "{value}"',
        "rows": [
            ["statement", fact.statement],
            ["value", value],
            ["kind", fact.kind],
            ["assertion", fact.assertion],
        ],
    }

    integration = {
        "key": "integration",
        "name": "Integration",
        "version": f"{integrator_version} · {integrate_version}",
        "summary": (
            f"resolved {fact.entity_ref} ({mode}) · "
            f"inferred {_bool(fact.inferred)} · self {fact.self_confidence:.2f}"
        ),
        "rows": [
            ["resolved", _resolution(fact.entity_ref, resolution)],
            ["predicate", _edge(fact)],
            ["inferred", _bool(fact.inferred)],
            ["supersession", supersession_action or "accumulate (no proposal)"],
            ["self_confidence", f"{fact.self_confidence:.2f}"],
        ],
    }

    # Surface-attested facts take the full ceiling outright; inferred ones are the
    # model's self-confidence bounded by it. Show whichever rule actually fired.
    weight_expr = (
        f"{planned.weight:.2f} (surface-attested → full ceiling)"
        if signals.surface_attested
        else f"min(self {fact.self_confidence:.2f}, ceiling {cap:.2f}) = {planned.weight:.2f}"
    )
    comparator = ">=" if planned.weight >= threshold else "<"
    reasons = ", ".join(planned.review_reasons) or "—"
    arbiter = {
        "key": "arbiter",
        "name": "Arbiter",
        "version": "weight model · deterministic",
        "summary": (
            f"ceiling {cap:.2f} · weight {planned.weight:.2f} "
            f"{comparator} {threshold:.2f} → {planned.status}"
        ),
        "rows": [
            ["surface_attested", _bool(signals.surface_attested)],
            ["predicate_known", _bool(signals.predicate_known)],
            ["is_supersede", _bool(signals.is_supersede)],
            ["ceiling", f"{cap:.2f}"],
            ["weight", weight_expr],
            ["threshold", f"{fact.kind} → {threshold:.2f}"],
            ["status", f"{planned.status} [{reasons}]"],
        ],
    }

    return {"stages": [extraction, integration, arbiter]}
