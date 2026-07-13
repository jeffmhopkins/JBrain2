"""Unit tests for the arbiter planning core (Wave 1 Track A, A1a).

Pure: the commit / review / reject disposition of an IntegrationIntent,
composing validate_intent (N3) and the weight model (N11).
"""

from datetime import datetime
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
    return ConfidenceSignals(surface_attested=True, is_supersede=False)


def _inferred_overwrite_sig():
    return ConfidenceSignals(surface_attested=False, is_supersede=True)


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


def test_compute_signals_named_object_attests_a_fumbled_quote():
    # The user's case: an edge the model marked stated (not inferred) whose own
    # attested_span quote isn't verbatim, but whose OBJECT org is literally named.
    # The object's presence attests the edge so it commits as `former` instead of
    # being held below threshold.
    intent = _intent(
        entity_resolutions=[
            _res("m1"),
            _res(
                "m2",
                mode="new",
                new_kind="Organization",
                new_name="Oregon Lithoprint",
                attested_span=AttestedSpan("c1", "Oregon Lithoprint"),
            ),
        ],
        facts=[
            _fact(
                entity_ref="m1",
                predicate="worksFor",
                kind="state",
                object_entity_ref="m2",
                attested_span=AttestedSpan("c1", "worked for Oregon Lithoprint"),  # not verbatim
                inferred=False,
            )
        ],
    )
    sig = compute_signals(intent, ["I used to work for the US army and Oregon Lithoprint."])
    assert sig[0].surface_attested is True


def test_compute_signals_named_object_rescues_an_inferred_relationship_edge():
    # The enumerated-kinship case: the integrator flags a `children` edge inferred
    # for the non-first members even though the note names each child. A
    # RELATIONSHIP edge whose object is named is grounded regardless of the
    # inferred flag (the relationship twin of date-phrase grounding), so it commits
    # instead of being held at the inferred ceiling — fixing the "50% on some
    # daughters" / missing reciprocal `parent` symptom.
    intent = _intent(
        entity_resolutions=[
            _res("Me"),
            _res("lydian", mode="new", new_kind="Person", new_name="lydian"),
        ],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="children",
                kind="relationship",
                object_entity_ref="lydian",
                attested_span=None,
                inferred=True,
            )
        ],
    )
    note = "I have four daughters named summer lydian Harmony and Elora"
    assert compute_signals(intent, [note])[0].surface_attested is True


def test_compute_signals_inferred_relationship_with_unnamed_object_stays_unattested():
    # Scope guard: the relationship backstop fires ONLY when the object is named.
    intent = _intent(
        entity_resolutions=[
            _res("Me"),
            _res("Eli", mode="new", new_kind="Person", new_name="Eli"),
        ],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="children",
                kind="relationship",
                object_entity_ref="Eli",
                attested_span=None,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["I have a kid."])[0].surface_attested is False


def test_derive_kinship_gender_adds_female_for_a_daughters_roster():
    from jbrain.analysis.arbiter import derive_kinship_gender

    # The model captured four children edges but omitted gender; "daughters" is the
    # only gendered child-term in the note → derive female for each child object.
    intent = _intent(
        entity_resolutions=[
            _res("Me"),
            _res("summer", mode="new", new_kind="Person", new_name="summer"),
            _res("lydian", mode="new", new_kind="Person", new_name="lydian"),
        ],
        facts=[
            _fact(entity_ref="Me", predicate="children", object_entity_ref="summer"),
            _fact(entity_ref="Me", predicate="children", object_entity_ref="lydian"),
        ],
    )
    out = derive_kinship_gender(intent, "I have two daughters named summer and lydian")
    genders = sorted(
        (f.entity_ref, (f.value_json or {}).get("value"), f.inferred)
        for f in out.facts
        if f.predicate == "gender"
    )
    assert genders == [("lydian", "female", True), ("summer", "female", True)]


def test_derive_kinship_gender_skips_a_mixed_gender_roster():
    from jbrain.analysis.arbiter import derive_kinship_gender

    # Both daughter and son terms present → can't associate positionally → derive
    # nothing for children, leaving it to the model/review.
    intent = _intent(
        entity_resolutions=[_res("Me"), _res("kid", mode="new", new_kind="Person", new_name="kid")],
        facts=[_fact(entity_ref="Me", predicate="children", object_entity_ref="kid")],
    )
    out = derive_kinship_gender(intent, "I have a daughter and a son.")
    assert [f for f in out.facts if f.predicate == "gender"] == []


def test_derive_kinship_gender_does_not_override_an_existing_gender_fact():
    from jbrain.analysis.arbiter import derive_kinship_gender

    existing = _fact(
        entity_ref="summer",
        predicate="gender",
        kind="state",
        value_json={"value": "male"},
        object_entity_ref=None,
    )
    intent = _intent(
        entity_resolutions=[
            _res("Me"),
            _res("summer", mode="new", new_kind="Person", new_name="summer"),
        ],
        facts=[_fact(entity_ref="Me", predicate="children", object_entity_ref="summer"), existing],
    )
    out = derive_kinship_gender(intent, "my daughter summer")
    summer_genders = [
        (f.value_json or {}).get("value")
        for f in out.facts
        if f.predicate == "gender" and f.entity_ref == "summer"
    ]
    assert summer_genders == ["male"]  # existing kept, no derived female added


def test_compute_signals_gender_grounded_by_a_kinship_term_attests():
    # "daughter ⇒ female" is a deterministic implication: an inferred gender fact
    # the note grounds with a gendered term is surface-attested, so it commits
    # instead of sitting in review like a guess.
    intent = _intent(
        entity_resolutions=[_res("m1")],
        facts=[
            _fact(
                predicate="gender",
                kind="state",
                value_json={"value": "female"},
                object_entity_ref=None,
                attested_span=None,
                inferred=True,
            )
        ],
    )
    note = "I have four daughters named summer lydian Harmony and Elora"
    assert compute_signals(intent, [note])[0].surface_attested is True


def test_compute_signals_gender_not_grounded_without_a_kinship_term():
    # No gendered term in the note → the inferred gender fact stays unattested.
    intent = _intent(
        entity_resolutions=[_res("m1")],
        facts=[
            _fact(
                predicate="gender",
                kind="state",
                value_json={"value": "female"},
                object_entity_ref=None,
                attested_span=None,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["Summer came over today."])[0].surface_attested is False


def test_compute_signals_gender_value_must_match_the_terms_gender():
    # A male gender fact is not grounded by female terms (and vice versa): the
    # value must match the gender the note's term implies.
    intent = _intent(
        entity_resolutions=[_res("m1")],
        facts=[
            _fact(
                predicate="gender",
                kind="state",
                value_json={"value": "male"},
                object_entity_ref=None,
                attested_span=None,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["I have two daughters."])[0].surface_attested is False


def test_compute_signals_appointment_time_grounds_via_clock_time_in_note():
    # `arriveBy` is a coined predicate (not date-shape) with no object, so neither
    # the named-object nor the date-phrase path applies — only _time_grounded: the
    # resolved 13:00 is printed in the note, so it's attested instead of held at 0.5.
    intent = _intent(
        entity_resolutions=[_res("appt")],
        facts=[
            _fact(
                entity_ref="appt",
                predicate="arriveBy",
                kind="state",
                object_entity_ref=None,
                attested_span=None,
                inferred=False,
                temporal=IntentTemporal(
                    phrase="arrive by",
                    resolved_start=datetime(2026, 7, 2, 13, 0),
                    resolved_end=None,
                    precision="instant",
                ),
            )
        ],
    )
    assert compute_signals(intent, ["Arrive by 13:00 for the appointment."])[0].surface_attested


def test_compute_signals_time_not_printed_in_note_is_not_grounded():
    intent = _intent(
        entity_resolutions=[_res("appt")],
        facts=[
            _fact(
                entity_ref="appt",
                predicate="arriveBy",
                kind="state",
                object_entity_ref=None,
                attested_span=None,
                inferred=True,
                temporal=IntentTemporal(
                    phrase="arrive by",
                    resolved_start=datetime(2026, 7, 2, 13, 0),
                    resolved_end=None,
                    precision="instant",
                ),
            )
        ],
    )
    assert compute_signals(intent, ["I have an appointment soon."])[0].surface_attested is False


def test_compute_signals_named_object_does_not_rescue_an_inferred_state_edge():
    # The `not inferred` gate still holds for a non-relationship (state) edge: a
    # genuinely inferred worksFor to a named org is NOT promoted — the model made
    # no honest claim the note states it, and only relationship edges get the
    # named-object override above.
    intent = _intent(
        entity_resolutions=[
            _res("m1"),
            _res(
                "m2",
                mode="new",
                new_kind="Organization",
                new_name="Globex",
                attested_span=AttestedSpan("c1", "Globex"),
            ),
        ],
        facts=[
            _fact(
                entity_ref="m1",
                predicate="worksFor",
                kind="state",
                object_entity_ref="m2",
                attested_span=None,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["I met someone from Globex."])[0].surface_attested is False


def test_compute_signals_named_object_absent_from_text_is_not_attested():
    # The object must be LITERALLY named: an edge to an object whose surface is
    # not in the note stays unattested (no fabricated attestation).
    intent = _intent(
        entity_resolutions=[
            _res("m1"),
            _res(
                "m2",
                mode="new",
                new_kind="Organization",
                new_name="Initech",
                attested_span=AttestedSpan("c1", "Initech"),
            ),
        ],
        facts=[
            _fact(
                entity_ref="m1",
                predicate="worksFor",
                kind="state",
                object_entity_ref="m2",
                attested_span=None,
                inferred=False,
            )
        ],
    )
    assert compute_signals(intent, ["I work at Globex now."])[0].surface_attested is False


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


def test_correction_marks_surface_attested_fact_active_full_weight() -> None:
    # A SURFACE-ATTESTED fact in a correction note commits active at full weight and is flagged
    # a correction (so the executor force-supersedes + pins).
    plan = plan_intent(
        _intent(entity_resolutions=[_res()], facts=[_fact()]),
        signals={0: _surface_sig()},
        correction=True,
    )
    pf = plan.facts[0]
    assert pf.weight == 1.0 and pf.status == "active" and pf.correction is True


def test_inferred_fact_in_correction_note_is_not_elevated() -> None:
    # An INFERRED fact inside a correction note must NOT be elevated or flagged a correction —
    # a hallucinated value can't bypass the inferred ceiling or force-supersede a confident prior.
    plan = plan_intent(
        _intent(entity_resolutions=[_res()], facts=[_fact(inferred=True, predicate="coined_pred")]),
        signals={0: ConfidenceSignals(False, True)},  # inferred, would-overwrite
        correction=True,
    )
    pf = plan.facts[0]
    assert pf.correction is False  # not a force-supersede
    assert pf.weight < 1.0 and pf.status == "pending_review"  # normal capped/review path


def test_correction_still_forced_to_review_by_a_safety_flag() -> None:
    # A correction is authoritative on weight, but a cross-subject link is still held for review.
    plan = plan_intent(
        _intent(
            entity_resolutions=[_res(cross_subject=True)],
            facts=[_fact()],
        ),
        correction=True,
    )
    pf = plan.facts[0]
    assert pf.status == "pending_review" and "cross_subject_link" in pf.review_reasons


def test_compute_signals_quote_drift_is_attested_after_normalization():
    # The model's attestation quote differs only by whitespace/casing from the
    # note — a clearly-stated fact must not be held just because the quote isn't
    # byte-identical (the run-to-run quote-drift that floods the review inbox).
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[_fact(attested_span=AttestedSpan("c1", "work   at  GLOBEX"), inferred=False)],
    )
    assert compute_signals(intent, ["I work at Globex now."])[0].surface_attested is True


def test_compute_signals_value_in_note_attests_an_attribute_without_a_quote():
    # An attribute (no object to fall back on) whose stored VALUE is literally in
    # the note is surface-attested even when the model omitted/paraphrased its
    # quote — the attribute twin of the named-object backstop.
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[
            _fact(
                predicate="grade",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "7th"},
                attested_span=None,
                inferred=False,
            )
        ],
    )
    assert compute_signals(intent, ["Eli, 12, going into 7th grade."])[0].surface_attested is True


def test_compute_signals_value_not_in_note_stays_unattested():
    # The value must actually appear: a value the note never states is not promoted.
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[
            _fact(
                predicate="grade",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "11th"},
                attested_span=None,
                inferred=False,
            )
        ],
    )
    assert compute_signals(intent, ["Eli, 12, going into 7th grade."])[0].surface_attested is False


def test_compute_signals_value_does_not_rescue_an_inferred_attribute():
    # The `not inferred` gate still governs: a value-in-note match never promotes a
    # fact the model itself flagged inferred (a guessed value the note happens to contain).
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[
            _fact(
                predicate="grade",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "7th"},
                attested_span=None,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["Eli, 12, going into 7th grade."])[0].surface_attested is False


def test_compute_signals_genuine_one_char_value_attests():
    # A legitimately STATED single-char value (a blood type "A") is attested — the
    # old length floor wrongly rejected it; standalone-token matching accepts it.
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[
            _fact(
                predicate="bloodType",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "A"},
                attested_span=None,
                inferred=False,
            )
        ],
    )
    assert compute_signals(intent, ["Blood type A, Rh positive."])[0].surface_attested is True


def test_compute_signals_in_word_value_match_does_not_attest():
    # The coincidental substring hit the length floor existed to block: value "7"
    # appears only INSIDE "$17", never as its own token, so it must not attest.
    intent = _intent(
        entity_resolutions=[_res()],
        facts=[
            _fact(
                predicate="grade",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "7"},
                attested_span=None,
                inferred=False,
            )
        ],
    )
    assert compute_signals(intent, ["Lunch was $17 today."])[0].surface_attested is False


def _extraction(facts, mentions):
    from jbrain.analysis.extraction import ExtractedFact, ExtractedMention, Extraction

    ef = [
        ExtractedFact(
            predicate=p,
            qualifier="",
            kind=k,
            statement="s",
            value_json=None,
            assertion="asserted",
            entity_ref=e,
            object_entity_ref=o,
            temporal=None,
            domain="general",
            confidence=0.9,
        )
        for (e, p, k, o) in facts
    ]
    em = [ExtractedMention(name=n, kind=kd, surface_text=n) for (n, kd) in mentions]
    return Extraction(title="t", tags=["x"], mentions=em, facts=ef, tokens=[])


def test_recover_backfills_a_dropped_object_and_resolution():
    from jbrain.analysis.arbiter import recover_dropped_fields

    # integrate dropped the object (None); extraction carried it.
    intent = _intent(
        entity_resolutions=[_res("Me", proposed_entity_id="ent-owner")],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="children",
                kind="relationship",
                object_entity_ref=None,
                inferred=False,
            )
        ],
    )
    ext = _extraction([("Me", "children", "relationship", "Eli")], [("Eli", "Person")])
    out = recover_dropped_fields(intent, ext)
    assert out.facts[0].object_entity_ref == "Eli"  # backfilled from extraction
    # and a provisional resolution was minted so apply_intent can link the edge
    eli = next(r for r in out.entity_resolutions if r.mention_ref == "Eli")
    assert eli.mode == "new" and eli.new_kind == "Person" and eli.new_name == "Eli"


def test_recover_distributes_objects_across_enumerated_set_valued_edges():
    from jbrain.analysis.arbiter import recover_dropped_fields

    # The integrator emitted four Me.children edges but dropped the object on
    # EVERY one (a set-valued predicate); extraction carried one object each.
    # Recovery must restore a DISTINCT child per edge — broadcasting the first
    # would collapse four edges into four copies of `summer`, which then de-dup to
    # one (the enumerated-kinship collapse this regression guards).
    kids = ["summer", "lydian", "Harmony", "Elora"]
    intent = _intent(
        entity_resolutions=[_res("Me", proposed_entity_id="ent-owner")],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="children",
                kind="relationship",
                object_entity_ref=None,
                inferred=False,
            )
            for _ in kids
        ],
    )
    ext = _extraction(
        [("Me", "children", "relationship", k) for k in kids],
        [(k, "Person") for k in kids],
    )
    out = recover_dropped_fields(intent, ext)
    objs = [f.object_entity_ref for f in out.facts]
    assert None not in objs  # no edge left orphaned
    assert sorted(o for o in objs if o is not None) == sorted(kids)  # one each, no collapse
    for k in kids:  # every child gets a provisional resolution so the edge links
        r = next(r for r in out.entity_resolutions if r.mention_ref == k)
        assert r.mode == "new" and r.new_kind == "Person"


def test_recover_does_not_reassign_an_object_a_sibling_edge_already_holds():
    from jbrain.analysis.arbiter import recover_dropped_fields

    # One edge kept its object (lydian); two were dropped. Recovery hands out only
    # the REMAINING extraction objects, never re-assigning lydian to a sibling.
    intent = _intent(
        entity_resolutions=[
            _res("Me", proposed_entity_id="ent-owner"),
            _res("lydian", mode="new", new_kind="Person", new_name="lydian"),
        ],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="children",
                kind="relationship",
                object_entity_ref="lydian",
            ),
            _fact(
                entity_ref="Me", predicate="children", kind="relationship", object_entity_ref=None
            ),
            _fact(
                entity_ref="Me", predicate="children", kind="relationship", object_entity_ref=None
            ),
        ],
    )
    kids = ["summer", "lydian", "Harmony"]
    ext = _extraction(
        [("Me", "children", "relationship", k) for k in kids],
        [(k, "Person") for k in kids],
    )
    out = recover_dropped_fields(intent, ext)
    objs = [f.object_entity_ref for f in out.facts]
    assert None not in objs
    assert sorted(o for o in objs if o is not None) == sorted(kids)


def test_recover_leaves_a_present_object_untouched():
    from jbrain.analysis.arbiter import recover_dropped_fields

    intent = _intent(
        entity_resolutions=[
            _res("Me", proposed_entity_id="ent-owner"),
            _res("Maya", mode="new", new_kind="Person", new_name="Maya"),
        ],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="spouse",
                kind="relationship",
                object_entity_ref="Maya",
                inferred=False,
            )
        ],
    )
    ext = _extraction([("Me", "spouse", "relationship", "Maya")], [("Maya", "Person")])
    out = recover_dropped_fields(intent, ext)
    assert out.facts[0].object_entity_ref == "Maya"
    # no duplicate resolution added (Maya already resolved)
    assert sum(r.mention_ref == "Maya" for r in out.entity_resolutions) == 1


def test_recover_drops_an_objectless_relationship_edge():
    from jbrain.analysis.arbiter import recover_dropped_fields

    # A `parent` edge with no object — and none to backfill from the extraction —
    # is an edge to nothing: dropped, not carried forward as an object-less row.
    # A non-relationship fact with no object (a goal) is unaffected.
    intent = _intent(
        entity_resolutions=[_res("Me")],
        facts=[
            _fact(entity_ref="Me", predicate="parent", kind="relationship", object_entity_ref=None),
            _fact(
                entity_ref="Me",
                predicate="goal",
                kind="preference",
                object_entity_ref=None,
                value_json={"value": "save more"},
            ),
        ],
    )
    out = recover_dropped_fields(intent, _extraction([], []))
    preds = [f.predicate for f in out.facts]
    assert "parent" not in preds  # object-less relationship dropped
    assert "goal" in preds  # object-less non-relationship kept


def test_recover_does_not_override_an_existing_ambiguous_resolution():
    from jbrain.analysis.arbiter import recover_dropped_fields

    intent = _intent(
        entity_resolutions=[
            _res("Me", proposed_entity_id="ent-owner"),
            EntityResolution(mention_ref="Sam", mode="ambiguous"),
        ],
        facts=[
            _fact(
                entity_ref="Me",
                predicate="friend",
                kind="relationship",
                object_entity_ref=None,
                inferred=False,
            )
        ],
    )
    ext = _extraction([("Me", "friend", "relationship", "Sam")], [("Sam", "Person")])
    out = recover_dropped_fields(intent, ext)
    assert out.facts[0].object_entity_ref == "Sam"  # ref restored
    sam = [r for r in out.entity_resolutions if r.mention_ref == "Sam"]
    assert len(sam) == 1 and sam[0].mode == "ambiguous"  # left as the integrator judged


def test_recover_backfills_a_dropped_value_json():
    from jbrain.analysis.arbiter import recover_dropped_fields

    # integrate re-typed the fact and blanked value_json; extraction carried it.
    intent = _intent(
        entity_resolutions=[_res("Eli", mode="new", new_kind="Person", new_name="Eli")],
        facts=[
            _fact(
                entity_ref="Eli",
                predicate="grade",
                kind="attribute",
                object_entity_ref=None,
                value_json=None,
                inferred=False,
            )
        ],
    )
    ext = _extraction([], [("Eli", "Person")])
    # add an extraction fact carrying the value
    from jbrain.analysis.extraction import ExtractedFact, Extraction

    ef = ExtractedFact(
        predicate="grade",
        qualifier="",
        kind="attribute",
        statement="s",
        value_json={"value": "7th"},
        assertion="asserted",
        entity_ref="Eli",
        object_entity_ref=None,
        temporal=None,
        domain="general",
        confidence=0.9,
    )
    ext = Extraction(title="t", tags=["x"], mentions=ext.mentions, facts=[ef], tokens=[])
    out = recover_dropped_fields(intent, ext)
    assert out.facts[0].value_json == {"value": "7th"}  # restored from extraction


def test_recover_backfills_a_dropped_temporal():
    from jbrain.analysis.arbiter import recover_dropped_fields

    # integrate re-typed the birthDate and stripped the age phrase it resolved from;
    # extraction carried the temporal -> restore it so _date_phrase_grounded can fire.
    intent = _intent(
        entity_resolutions=[_res("Eli", mode="new", new_kind="Person", new_name="Eli")],
        facts=[
            _fact(
                entity_ref="Eli",
                predicate="birthDate",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "2013"},
                temporal=None,
                inferred=True,
            )
        ],
    )
    from jbrain.analysis.extraction import (
        ExtractedFact,
        ExtractedMention,
        ExtractedTemporal,
        Extraction,
    )

    t = ExtractedTemporal(
        phrase="12", resolved_start=datetime(2013, 1, 1), resolved_end=None, precision="year"
    )
    ef = ExtractedFact(
        predicate="birthDate",
        qualifier="",
        kind="attribute",
        statement="s",
        value_json={"value": "2013"},
        assertion="asserted",
        entity_ref="Eli",
        object_entity_ref=None,
        temporal=t,
        domain="general",
        confidence=0.9,
    )
    ext = Extraction(
        title="t",
        tags=["x"],
        mentions=[ExtractedMention(name="Eli", kind="Person", surface_text="Eli")],
        facts=[ef],
        tokens=[],
    )
    out = recover_dropped_fields(intent, ext)
    assert out.facts[0].temporal is not None  # restored from extraction
    assert out.facts[0].temporal.phrase == "12"
    assert out.facts[0].temporal.resolved_start == datetime(2013, 1, 1)


def test_recover_then_ground_commits_an_inferred_birthdate_active():
    # The whole chain the box exercises, deterministically: integrate dropped the
    # birthDate's temporal (age phrase), so without recovery the inferred fact is
    # held. recover restores the temporal from the extraction -> compute_signals
    # grounds it (date-shape + phrase in note) -> assess commits it active.
    from jbrain.analysis.arbiter import compute_signals, recover_dropped_fields
    from jbrain.analysis.extraction import (
        ExtractedFact,
        ExtractedMention,
        ExtractedTemporal,
        Extraction,
    )
    from jbrain.analysis.weight import assess

    body = "Eli, 12, going into 7th grade."
    intent = _intent(
        entity_resolutions=[_res("Eli", mode="new", new_kind="Person", new_name="Eli")],
        facts=[
            _fact(
                entity_ref="Eli",
                predicate="birthDate",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "2013"},
                temporal=None,
                inferred=True,
            )
        ],
    )
    t = ExtractedTemporal(
        phrase="12", resolved_start=datetime(2013, 1, 1), resolved_end=None, precision="year"
    )
    ef = ExtractedFact(
        predicate="birthDate",
        qualifier="",
        kind="attribute",
        statement="s",
        value_json={"value": "2013"},
        assertion="asserted",
        entity_ref="Eli",
        object_entity_ref=None,
        temporal=t,
        domain="general",
        confidence=0.9,
    )
    ext = Extraction(
        title="t",
        tags=["x"],
        mentions=[ExtractedMention(name="Eli", kind="Person", surface_text="Eli")],
        facts=[ef],
        tokens=[],
    )

    recovered = recover_dropped_fields(intent, ext)
    sig = compute_signals(recovered, [body])[0]
    assert sig.surface_attested is True
    _w, status = assess(recovered.facts[0].kind, recovered.facts[0].self_confidence, sig)
    assert status == "active"  # no longer held for review


def test_date_phrase_in_note_attests_an_inferred_birthdate():
    from jbrain.analysis.arbiter import compute_signals

    # birthDate derived from a stated age: inferred=True, but the age phrase "12"
    # is in the note and the predicate is date-shape -> grounded -> attested.
    t = IntentTemporal(
        phrase="12", resolved_start=datetime(2013, 1, 1), resolved_end=None, precision="year"
    )
    intent = _intent(
        entity_resolutions=[_res("Eli", mode="new", new_kind="Person", new_name="Eli")],
        facts=[
            _fact(
                entity_ref="Eli",
                predicate="birthDate",
                kind="attribute",
                object_entity_ref=None,
                value_json={"value": "2013"},
                temporal=t,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["Eli, 12, going into 7th grade."])[0].surface_attested is True


def test_date_grounding_is_scoped_to_date_predicates():
    from jbrain.analysis.arbiter import compute_signals

    # A NON-date inferred fact with a note-present temporal phrase is NOT promoted
    # (a stated timestamp doesn't attest a guessed value).
    t = IntentTemporal(
        phrase="Tuesday", resolved_start=datetime(2026, 6, 16), resolved_end=None, precision="day"
    )
    intent = _intent(
        entity_resolutions=[_res("m1")],
        facts=[
            _fact(
                entity_ref="m1",
                predicate="jobTitle",
                kind="attribute",
                object_entity_ref=None,
                value_json=None,
                temporal=t,
                inferred=True,
            )
        ],
    )
    assert compute_signals(intent, ["Saw them Tuesday."])[0].surface_attested is False


# --- dedup_intent_facts (the medication-bite-review duplicate) --------------
#
# The production shape (confirmed on the box): a medication is `Me.medication ->
# <drug>` with the drug as an OBJECT entity, which `_object_named` grounds because
# the drug name is verbatim in the note. The Integrator's spurious duplicate DROPS
# the object and folds the drug into the free-text statement — that object-less twin
# is the copy that lands in review.


def _med_fact(statement: str, *, obj: str | None, self_confidence: float = 0.5) -> IntentFact:
    return _fact(
        entity_ref="Me",
        predicate="medication",
        kind="state",
        object_entity_ref=obj,
        value_json=None,
        statement=statement,
        # The bound copy quotes nothing special; it grounds on its named object. The
        # object-less twin carries no span either — its problem is the missing object.
        attested_span=None,
        self_confidence=self_confidence,
        inferred=False,
    )


def test_dedup_intent_facts_subsumes_the_object_dropped_duplicate_medication():
    from jbrain.analysis.arbiter import dedup_intent_facts

    note = "The owner is taking lisinopril 10 mg and hydrochlorothiazide 12.5 mg daily."
    # Exactly the production intent: lisinopril bound, hydrochlorothiazide bound, and
    # a spurious object-less hydrochlorothiazide twin.
    lisinopril = _med_fact("The owner is taking lisinopril 10 mg daily.", obj="lisinopril")
    hctz_bound = _med_fact(
        "The owner is taking hydrochlorothiazide 12.5 mg daily.", obj="hydrochlorothiazide"
    )
    hctz_dropped = _med_fact("The owner is taking hydrochlorothiazide 12.5 mg daily.", obj=None)
    resolutions = [
        _res("Me"),
        _res(
            "lisinopril",
            mode="new",
            new_kind="Drug",
            new_name="lisinopril",
            attested_span=AttestedSpan("c1", "lisinopril"),
        ),
        _res(
            "hydrochlorothiazide",
            mode="new",
            new_kind="Drug",
            new_name="hydrochlorothiazide",
            attested_span=AttestedSpan("c1", "hydrochlorothiazide"),
        ),
    ]
    intent = _intent(entity_resolutions=resolutions, facts=[lisinopril, hctz_bound, hctz_dropped])
    out = dedup_intent_facts(intent, [note])
    # Both distinct drugs survive as bound edges; the object-less twin is gone.
    assert len(out.facts) == 2
    assert all(f.object_entity_ref is not None for f in out.facts)
    objs = sorted(f.object_entity_ref for f in out.facts if f.object_entity_ref is not None)
    assert objs == ["hydrochlorothiazide", "lisinopril"]
    # The surviving hydrochlorothiazide edge grounds on its named object, so the
    # arbiter now commits it active instead of holding the dropped twin for review.
    sig = compute_signals(out, [note])
    hctz_i = next(
        i for i, f in enumerate(out.facts) if f.object_entity_ref == "hydrochlorothiazide"
    )
    assert sig[hctz_i].surface_attested is True


def test_dedup_intent_facts_keeps_distinct_object_edges_even_with_identical_statements():
    from jbrain.analysis.arbiter import dedup_intent_facts

    # The enumerated-kinship guard: two children edges to DIFFERENT objects must both
    # survive even if the model wrote them the same generic statement — the base key
    # excludes the object, so the distinct-object arm is what keeps them apart.
    note = "My kids."
    a = _fact(
        entity_ref="Me",
        predicate="children",
        kind="relationship",
        object_entity_ref="Eli",
        statement="My child.",
        attested_span=None,
        inferred=False,
    )
    b = _fact(
        entity_ref="Me",
        predicate="children",
        kind="relationship",
        object_entity_ref="Nora",
        statement="My child.",
        attested_span=None,
        inferred=False,
    )
    out = dedup_intent_facts(_intent(entity_resolutions=[_res("Me")], facts=[a, b]), [note])
    objs = sorted(f.object_entity_ref for f in out.facts if f.object_entity_ref is not None)
    assert objs == ["Eli", "Nora"]


def test_dedup_intent_facts_collapses_identical_object_less_free_text_facts():
    from jbrain.analysis.arbiter import dedup_intent_facts

    # When the model never bound the drug at all, two byte-identical object-less
    # copies still collapse to the best one (higher self_confidence here).
    note = "Taking hydrochlorothiazide 12.5 mg daily."
    a = _med_fact("The owner takes hydrochlorothiazide.", obj=None, self_confidence=0.4)
    b = _med_fact("The owner takes hydrochlorothiazide.", obj=None, self_confidence=0.6)
    out = dedup_intent_facts(_intent(entity_resolutions=[_res("Me")], facts=[a, b]), [note])
    assert len(out.facts) == 1
    assert out.facts[0].self_confidence == 0.6


def test_dedup_intent_facts_does_not_collapse_distinct_set_valued_members():
    from jbrain.analysis.arbiter import dedup_intent_facts

    note = "The owner is taking lisinopril 10 mg and hydrochlorothiazide 12.5 mg daily."
    a = _med_fact("The owner is taking lisinopril 10 mg daily.", obj="lisinopril")
    b = _med_fact(
        "The owner is taking hydrochlorothiazide 12.5 mg daily.", obj="hydrochlorothiazide"
    )
    # Different drugs, different statements AND objects: both are real, neither collapses.
    out = dedup_intent_facts(_intent(entity_resolutions=[_res("Me")], facts=[a, b]), [note])
    assert len(out.facts) == 2


def test_dedup_intent_facts_is_a_noop_without_duplicates():
    from jbrain.analysis.arbiter import dedup_intent_facts

    intent = _intent(entity_resolutions=[_res("m1")], facts=[_fact()])
    # Identity is preserved (same object) when nothing collapses — cheap and
    # side-effect free for the common case.
    assert dedup_intent_facts(intent, ["married to Celine"]) is intent


def test_dedup_intent_facts_distinguishes_by_value_json():
    from jbrain.analysis.arbiter import dedup_intent_facts

    # Same key + same statement but different structured value are different data
    # (a defensive guard on the value_json arm of the base key).
    a = _fact(predicate="weight", kind="measurement", value_json={"value": 178, "unit": "lb"})
    b = _fact(predicate="weight", kind="measurement", value_json={"value": 180, "unit": "lb"})
    out = dedup_intent_facts(_intent(entity_resolutions=[_res()], facts=[a, b]), [""])
    assert len(out.facts) == 2
