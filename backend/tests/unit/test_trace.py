"""The review-card process trace projection (analysis.trace.build_trace)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from jbrain.analysis.arbiter import PlannedFact
from jbrain.analysis.intent import EntityResolution, IntentFact
from jbrain.analysis.trace import build_trace
from jbrain.analysis.weight import ConfidenceSignals, assess

_BASE_FACT = IntentFact(
    entity_ref="Me",
    predicate="name.full",
    qualifier="",
    kind="state",
    statement="My full name is Jeffrey Mark Hopkins.",
    value_json={"value": "Jeffrey Mark Hopkins"},
    assertion="asserted",
    object_entity_ref=None,
    temporal=None,
    attested_span=None,
    self_confidence=0.85,
    inferred=True,
)
_DEFAULT_RESOLUTION = EntityResolution(
    mention_ref="Me", mode="existing", proposed_entity_id="owner-1"
)


def _fact(**over: Any) -> IntentFact:
    return replace(_BASE_FACT, **over)


def _planned(fact: IntentFact, signals: ConfidenceSignals) -> PlannedFact:
    weight, status = assess(fact.kind, fact.self_confidence, signals)
    reasons = ("below_threshold",) if status == "pending_review" else ()
    return PlannedFact(fact=fact, weight=weight, status=status, review_reasons=reasons)


def _trace(
    fact: IntentFact,
    signals: ConfidenceSignals,
    *,
    resolution: EntityResolution | None = _DEFAULT_RESOLUTION,
    supersession_action: str | None = None,
) -> dict[str, Any]:
    return build_trace(
        fact,
        _planned(fact, signals),
        signals,
        resolution=resolution,
        supersession_action=supersession_action,
        extract_version="note-extract-v16",
        integrate_version="integrate-v7",
        integrator_version="integrator-v2",
    )


def _rows(stage: dict) -> dict[str, str]:
    return {k: v for k, v in stage["rows"]}


def test_trace_has_the_three_named_stages_in_order() -> None:
    fact = _fact()
    sig = ConfidenceSignals(surface_attested=False, is_supersede=False)
    stages = _trace(fact, sig)["stages"]
    assert [s["key"] for s in stages] == ["extraction", "integration", "arbiter"]
    assert [s["name"] for s in stages] == ["Extraction", "Integration", "Arbiter"]
    # Versions stamp provenance so a re-run is traceable to the prompt that made it.
    assert stages[0]["version"] == "note-extract-v16"
    assert stages[1]["version"] == "integrator-v2 · integrate-v7"


def test_inferred_below_threshold_shows_the_ceiling_arithmetic() -> None:
    # The screenshot case: an inferred attribute (self 0.85) capped at the 0.60
    # inferred ceiling, held because 0.60 < the 0.80 attribute threshold.
    fact = _fact(kind="attribute")
    sig = ConfidenceSignals(surface_attested=False, is_supersede=False)
    arb = _rows([s for s in _trace(fact, sig)["stages"] if s["key"] == "arbiter"][0])
    assert arb["surface_attested"] == "false"
    assert arb["ceiling"] == "0.60"
    assert arb["weight"] == "min(self 0.85, ceiling 0.60) = 0.60"
    assert arb["threshold"] == "attribute → 0.80"
    assert arb["status"] == "pending_review [below_threshold]"


def test_surface_attested_fact_takes_the_full_ceiling() -> None:
    fact = _fact(kind="attribute", inferred=False)
    sig = ConfidenceSignals(surface_attested=True, is_supersede=False)
    arb = _rows([s for s in _trace(fact, sig)["stages"] if s["key"] == "arbiter"][0])
    assert arb["ceiling"] == "1.00"
    assert arb["weight"] == "1.00 (surface-attested → full ceiling)"


def test_integration_stage_carries_resolution_and_supersession() -> None:
    fact = _fact()
    sig = ConfidenceSignals(surface_attested=False, is_supersede=True)
    integ = _rows(
        [
            s
            for s in _trace(
                fact,
                sig,
                resolution=EntityResolution(mention_ref="Me", mode="new", new_kind="Person"),
                supersession_action="supersede",
            )["stages"]
            if s["key"] == "integration"
        ][0]
    )
    assert integ["resolved"] == "Me → new Person"
    assert integ["inferred"] == "true"
    assert integ["supersession"] == "supersede"
    assert integ["self_confidence"] == "0.85"


def test_qualifier_is_shown_on_the_predicate_edge() -> None:
    fact = _fact(predicate="name.nickname", qualifier="kids", value_json={"value": "Dad"})
    sig = ConfidenceSignals(surface_attested=True, is_supersede=False)
    integ = _rows([s for s in _trace(fact, sig)["stages"] if s["key"] == "integration"][0])
    assert integ["predicate"] == "name.nickname.kids"


def test_missing_resolution_reads_unresolved() -> None:
    fact = _fact()
    sig = ConfidenceSignals(surface_attested=False, is_supersede=False)
    integ = _rows(
        [s for s in _trace(fact, sig, resolution=None)["stages"] if s["key"] == "integration"][0]
    )
    assert integ["resolved"] == "Me → unresolved"
    assert integ["supersession"] == "accumulate (no proposal)"
