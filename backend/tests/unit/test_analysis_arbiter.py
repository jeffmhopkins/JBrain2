"""Unit tests for the arbiter planning core (Wave 1 Track A, A1a).

Pure: the commit / review / reject disposition of an IntegrationIntent,
composing validate_intent (N3) and the weight model (N11).
"""

from typing import Any

import pytest

from jbrain.analysis.arbiter import compute_signals, plan_intent, plan_to_extraction
from jbrain.analysis.intent import (
    AttestedSpan,
    EntityPairProposal,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    IntentTemporal,
    SupersessionProposal,
)
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.schema import get_registry


def _res(ref: str = "m1", **kw) -> EntityResolution:
    base: dict[str, Any] = dict(
        mode="existing", proposed_entity_id="e1", attested_span=AttestedSpan("c1", "Celine")
    )
    base.update(kw)
    return EntityResolution(mention_ref=ref, **base)


def _fact(entity_ref: str = "m1", **kw) -> IntentFact:
    base: dict[str, Any] = dict(
        predicate="spouse",
        qualifier="",
        kind="relationship",
        statement="married to Celine",
        value_json=None,
        assertion="asserted",
        object_entity_ref=None,
        temporal=None,
        attested_span=AttestedSpan("c1", "wife Celine"),
        self_confidence=0.9,
        inferred=False,
    )
    base.update(kw)
    return IntentFact(entity_ref=entity_ref, **base)


def _intent(**kw) -> IntegrationIntent:
    base: dict[str, Any] = dict(
        note_id="n1", schema_version=1, prompt_version="v13", integrator_version="i1"
    )
    base.update(kw)
    return IntegrationIntent(**base)


def _surface_sig():
    return ConfidenceSignals(surface_attested=True, predicate_known=True, is_supersede=False)


def _inferred_overwrite_sig():
    return ConfidenceSignals(surface_attested=False, predicate_known=True, is_supersede=True)


def test_fatal_violation_rejects_whole_intent():
    # A fact referencing a non-existent mention is a fatal structural error.
    plan = plan_intent(_intent(entity_resolutions=[_res("m1")], facts=[_fact(entity_ref="ghost")]))
    assert plan.rejected is True
    assert plan.facts == ()
    assert any(v.code == "unknown_entity_ref" for v in plan.fatal_violations)


def test_surface_attested_fact_commits():
    plan = plan_intent(
        _intent(entity_resolutions=[_res()], facts=[_fact()]),
        signals={0: _surface_sig()},
    )
    assert plan.rejected is False
    assert len(plan.to_commit) == 1
    assert plan.to_commit[0].status == "active"
    assert plan.to_commit[0].weight == 1.0  # surface-attested → full ceiling


def test_inferred_attribute_overwrite_routes_to_review():
    # The canonical case: a pronoun-inferred gender that would overwrite. The
    # weight ceiling (0.4) is far below attribute's 0.8 threshold → review.
    plan = plan_intent(
        _intent(
            entity_resolutions=[_res()],
            facts=[_fact(predicate="gender", kind="attribute", inferred=True, attested_span=None)],
        ),
        signals={0: _inferred_overwrite_sig()},
    )
    assert plan.to_commit == ()
    assert len(plan.to_review) == 1
    assert plan.to_review[0].status == "pending_review"


def test_cross_subject_link_forces_fact_to_review_despite_high_weight():
    plan = plan_intent(
        _intent(entity_resolutions=[_res(cross_subject=True)], facts=[_fact()]),
        signals={0: _surface_sig()},  # weight would otherwise commit
    )
    assert plan.to_commit == ()
    assert plan.to_review[0].review_reasons == ("cross_subject_link",)


def test_ambiguous_mention_forces_fact_to_review():
    amb = EntityResolution(mention_ref="m1", mode="ambiguous")
    plan = plan_intent(
        _intent(entity_resolutions=[amb], facts=[_fact()]), signals={0: _surface_sig()}
    )
    assert plan.to_review[0].review_reasons == ("ambiguous_mention",)


def test_object_ref_flag_also_forces_review():
    # A relationship whose OBJECT mention is cross-subject also holds for review.
    plan = plan_intent(
        _intent(
            entity_resolutions=[_res("m1"), _res("m2", cross_subject=True)],
            facts=[_fact(entity_ref="m1", object_entity_ref="m2")],
        ),
        signals={0: _surface_sig()},
    )
    assert plan.to_review[0].review_reasons == ("cross_subject_link",)


def test_missing_signals_default_conservative():
    # No signals supplied → conservative (inferred/unknown/overwrite) → low
    # weight → review, never a silent commit.
    plan = plan_intent(_intent(entity_resolutions=[_res()], facts=[_fact()]))
    assert plan.to_commit == ()
    assert len(plan.to_review) == 1


def test_merge_and_distinct_proposals_carried_for_review():
    plan = plan_intent(
        _intent(
            merge_proposals=[EntityPairProposal("e1", "e2")],
            distinct_proposals=[EntityPairProposal("e3", "e4")],
        )
    )
    assert len(plan.merge_proposals) == 1
    assert len(plan.distinct_proposals) == 1


def test_weight_gated_review_records_below_threshold_reason():
    plan = plan_intent(
        _intent(
            entity_resolutions=[_res()],
            facts=[_fact(predicate="gender", kind="attribute", inferred=True, attested_span=None)],
        ),
        signals={0: _inferred_overwrite_sig()},
    )
    assert plan.to_review[0].review_reasons == ("below_threshold",)


def test_self_edge_flag_reason_is_not_duplicated():
    # Same flagged mention as both subject and object → one reason, not two.
    plan = plan_intent(
        _intent(
            entity_resolutions=[_res(cross_subject=True)],
            facts=[_fact(entity_ref="m1", object_entity_ref="m1")],
        ),
        signals={0: _surface_sig()},
    )
    assert plan.to_review[0].review_reasons == ("cross_subject_link",)


def test_review_severity_violation_does_not_reject():
    # A surface fact with no span is a REVIEW-level violation, not fatal: the
    # intent is not rejected (only fatal violations hold the whole intent).
    plan = plan_intent(
        _intent(entity_resolutions=[_res()], facts=[_fact(attested_span=None)]),
        signals={0: _surface_sig()},
    )
    assert plan.rejected is False


def test_mixed_intent_partitions_correctly():
    plan = plan_intent(
        _intent(
            entity_resolutions=[_res("m1"), _res("m2", cross_subject=True)],
            facts=[
                _fact(entity_ref="m1"),  # surface → commit
                _fact(entity_ref="m2"),  # cross-subject → review
            ],
        ),
        signals={0: _surface_sig(), 1: _surface_sig()},
    )
    assert len(plan.to_commit) == 1
    assert len(plan.to_review) == 1
    assert plan.to_commit[0].fact.entity_ref == "m1"
    assert plan.to_review[0].fact.entity_ref == "m2"


def test_plan_to_extraction_maps_mentions_facts_and_weight():
    intent = _intent(entity_resolutions=[_res("m1")], facts=[_fact(self_confidence=0.95)])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    extraction = plan_to_extraction(intent, plan, title="A day", tags=["life"])

    assert extraction.title == "A day"
    assert extraction.tags == ["life"]
    # Mentions/facts are keyed by mention_ref (Option 1 bridge).
    assert [m.name for m in extraction.mentions] == ["m1"]
    assert len(extraction.facts) == 1
    ef = extraction.facts[0]
    assert ef.entity_ref == "m1"
    assert ef.predicate == "spouse"
    # confidence is the deterministic plan weight, not the model's self-report.
    assert ef.confidence == plan.to_commit[0].weight
    # domain is deferred to the note's domain in _upsert_fact.
    assert ef.domain == ""


def test_plan_to_extraction_includes_review_facts_too():
    # Both committed and review-held facts must be written (review as pending).
    intent = _intent(
        entity_resolutions=[_res("m1"), _res("m2", cross_subject=True)],
        facts=[_fact(entity_ref="m1"), _fact(entity_ref="m2")],
    )
    plan = plan_intent(intent, signals={0: _surface_sig(), 1: _surface_sig()})
    extraction = plan_to_extraction(intent, plan)
    assert {f.entity_ref for f in extraction.facts} == {"m1", "m2"}


def test_plan_to_extraction_maps_temporal():
    from datetime import UTC, datetime

    when = datetime(2021, 6, 1, tzinfo=UTC)
    temporal = IntentTemporal(
        phrase="last June", resolved_start=when, resolved_end=None, precision="month"
    )
    intent = _intent(entity_resolutions=[_res("m1")], facts=[_fact(temporal=temporal)])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    ef = plan_to_extraction(intent, plan).facts[0]
    assert ef.temporal is not None
    assert ef.temporal.phrase == "last June"
    assert ef.temporal.resolved_start == when
    assert ef.temporal.precision == "month"


def test_plan_to_extraction_uses_new_kind_for_minted_mention():
    res = EntityResolution(mention_ref="m9", mode="new", new_kind="Organization", new_name="Globex")
    intent = _intent(entity_resolutions=[res], facts=[_fact(entity_ref="m9")])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    mention = plan_to_extraction(intent, plan).mentions[0]
    assert mention.kind == "Organization"


def test_plan_to_extraction_rejects_a_rejected_plan():
    plan = plan_intent(_intent(entity_resolutions=[_res("m1")], facts=[_fact(entity_ref="ghost")]))
    assert plan.rejected
    with pytest.raises(ValueError):
        plan_to_extraction(_intent(), plan)


def test_plan_to_extraction_round_trips_all_fact_fields():
    # Guard against a silent _to_extracted field-drop: value_json, object ref,
    # qualifier, assertion must survive the bridge.
    intent = _intent(
        entity_resolutions=[_res("m1"), _res("m2")],
        facts=[
            _fact(
                entity_ref="m1",
                predicate="weight",
                qualifier="morning",
                kind="measurement",
                assertion="reported",
                value_json={"value": 182, "unit": "lb"},
                object_entity_ref="m2",
            )
        ],
    )
    plan = plan_intent(intent, signals={0: _surface_sig()})
    ef = plan_to_extraction(intent, plan).facts[0]
    assert ef.qualifier == "morning"
    assert ef.kind == "measurement"
    assert ef.assertion == "reported"
    assert ef.value_json == {"value": 182, "unit": "lb"}
    assert ef.object_entity_ref == "m2"


def test_plan_to_extraction_review_fact_carries_capped_weight():
    # A below-threshold fact's confidence is its capped plan weight, not the
    # model's (higher) self_confidence.
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(kind="attribute", inferred=True, attested_span=None, self_confidence=0.99)],
    )
    plan = plan_intent(intent, signals={0: _inferred_overwrite_sig()})
    ef = plan_to_extraction(intent, plan).facts[0]
    assert ef.confidence == plan.to_review[0].weight
    assert ef.confidence < 0.99


def test_plan_to_extraction_surface_fallback_to_mention_ref():
    res = EntityResolution(mention_ref="m1", mode="existing", proposed_entity_id="e1")  # no span
    intent = _intent(entity_resolutions=[res], facts=[_fact(attested_span=None, inferred=True)])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    assert plan_to_extraction(intent, plan).mentions[0].surface_text == "m1"


def test_plan_to_extraction_existing_resolution_kind_is_thing():
    intent = _intent(entity_resolutions=[_res("m1")], facts=[_fact()])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    assert plan_to_extraction(intent, plan).mentions[0].kind == "Thing"


def test_plan_to_extraction_threads_dropped_facts_for_truncation_card():
    # W0: the per-note cap fires upstream on the (uncapped) extract, but the
    # intent/plan only ever see the already-capped list. The rebuilt Extraction
    # must carry the upstream drop count forward so the pipeline still files the
    # extraction_truncated card — before the fix this defaulted to 0 and the
    # card was silently never filed for a clipped long note.
    intent = _intent(entity_resolutions=[_res("m1")], facts=[_fact()])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    assert plan_to_extraction(intent, plan, dropped_facts=7).dropped_facts == 7


def test_plan_to_extraction_dropped_facts_defaults_to_zero():
    # An under-cap note (no upstream drop) carries 0, so _sync_truncation_review
    # files no card — the standalone/eval call path stays card-free.
    intent = _intent(entity_resolutions=[_res("m1")], facts=[_fact()])
    plan = plan_intent(intent, signals={0: _surface_sig()})
    assert plan_to_extraction(intent, plan).dropped_facts == 0


def test_plan_to_extraction_commit_only_excludes_review_facts():
    # commit_only drops review-held facts (cross-subject here) but keeps every
    # mention — the A1b-ii-1 safety so a high-weight review fact can't commit.
    intent = _intent(
        entity_resolutions=[_res("m1"), _res("m2", cross_subject=True)],
        facts=[_fact(entity_ref="m1"), _fact(entity_ref="m2")],
    )
    plan = plan_intent(intent, signals={0: _surface_sig(), 1: _surface_sig()})
    ex = plan_to_extraction(intent, plan, commit_only=True)
    assert [f.entity_ref for f in ex.facts] == ["m1"]
    assert len(ex.mentions) == 2


def test_compute_signals_surface_attested_requires_present_and_not_inferred():
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(attested_span=AttestedSpan("c1", "Globex"), inferred=False)],
    )
    assert compute_signals(intent, ["I work at Globex now."])[0].surface_attested is True


def test_compute_signals_inferred_is_not_surface_attested():
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(attested_span=AttestedSpan("c1", "Globex"), inferred=True)],
    )
    assert compute_signals(intent, ["I work at Globex now."])[0].surface_attested is False


def test_compute_signals_surface_not_in_text_is_not_attested():
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(attested_span=AttestedSpan("c1", "Initech"), inferred=False)],
    )
    assert compute_signals(intent, ["I work at Globex now."])[0].surface_attested is False


def test_compute_signals_predicate_known_matches_registry():
    reg = get_registry()

    def known(pred: str) -> bool:
        return any(t.predicate(pred) is not None for t in reg.types.values())

    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(predicate="zzz_made_up_predicate"), _fact(predicate="spouse")],
    )
    sigs = compute_signals(intent, ["x"])
    assert sigs[0].predicate_known is False  # a coined long-tail predicate
    assert sigs[1].predicate_known == known("spouse")  # matches the registry, no hardcoding


def test_compute_signals_is_supersede_from_agent_proposal():
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(predicate="employer")],
        supersession_proposals=[
            SupersessionProposal(
                entity_ref="m1", predicate="employer", qualifier="", action="supersede"
            )
        ],
    )
    assert compute_signals(intent, ["x"])[0].is_supersede is True


def test_compute_signals_no_supersede_proposal_is_false():
    intent = _intent(entity_resolutions=[_res()], facts=[_fact(predicate="employer")])
    assert compute_signals(intent, ["x"])[0].is_supersede is False
