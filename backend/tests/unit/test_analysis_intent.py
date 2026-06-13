"""Unit tests for the IntegrationIntent seam + its structural validator.

Pure (no DB): these guard the contract's invariants — the agent emits intent,
the arbiter validates structure before trusting it.
"""

from jbrain.analysis.intent import (
    AttestedSpan,
    EntityPairProposal,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    SupersessionProposal,
    has_fatal,
    validate_intent,
)


def _resolution(ref: str = "m1", **kw) -> EntityResolution:
    base = dict(
        mode="existing", proposed_entity_id="e1", attested_span=AttestedSpan("c1", "Celine")
    )
    base.update(kw)
    return EntityResolution(mention_ref=ref, **base)


def _fact(entity_ref: str = "m1", **kw) -> IntentFact:
    base = dict(
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
    base = dict(note_id="n1", schema_version=1, prompt_version="v13", integrator_version="i1")
    base.update(kw)
    return IntegrationIntent(**base)


def test_clean_intent_has_no_violations():
    intent = _intent(entity_resolutions=[_resolution()], facts=[_fact()])
    assert validate_intent(intent) == []


def test_fact_referencing_unknown_mention_is_fatal():
    intent = _intent(entity_resolutions=[_resolution("m1")], facts=[_fact(entity_ref="ghost")])
    v = validate_intent(intent)
    assert has_fatal(v)
    assert any(x.code == "unknown_entity_ref" for x in v)


def test_unknown_object_ref_is_fatal():
    intent = _intent(entity_resolutions=[_resolution("m1")], facts=[_fact(object_entity_ref="m2")])
    assert any(
        x.code == "unknown_object_ref" and x.severity == "fatal" for x in validate_intent(intent)
    )


def test_bad_kind_and_assertion_are_fatal():
    intent = _intent(
        entity_resolutions=[_resolution()], facts=[_fact(kind="nonsense", assertion="maybe")]
    )
    codes = {x.code for x in validate_intent(intent) if x.severity == "fatal"}
    assert {"bad_kind", "bad_assertion"} <= codes


def test_confidence_out_of_range_is_fatal():
    intent = _intent(entity_resolutions=[_resolution()], facts=[_fact(self_confidence=1.4)])
    assert any(x.code == "bad_confidence" for x in validate_intent(intent))


def test_existing_resolution_without_entity_id_is_fatal():
    bad = EntityResolution(mention_ref="m1", mode="existing", proposed_entity_id=None)
    intent = _intent(entity_resolutions=[bad])
    assert any(x.code == "resolution_missing_entity" for x in validate_intent(intent))


def test_new_resolution_without_kind_and_name_is_fatal():
    bad = EntityResolution(mention_ref="m1", mode="new")
    intent = _intent(entity_resolutions=[bad])
    assert any(x.code == "resolution_missing_new" for x in validate_intent(intent))


def test_ambiguous_resolution_is_review_not_fatal():
    amb = EntityResolution(mention_ref="m1", mode="ambiguous")
    v = validate_intent(_intent(entity_resolutions=[amb]))
    assert not has_fatal(v)
    assert any(x.code == "ambiguous_mention" and x.severity == "review" for x in v)


def test_cross_subject_link_is_staged_for_review():
    r = _resolution(cross_subject=True)
    v = validate_intent(_intent(entity_resolutions=[r], facts=[_fact()]))
    assert not has_fatal(v)
    assert any(x.code == "cross_subject_link" and x.severity == "review" for x in v)


def test_surface_fact_without_span_is_review():
    # inferred=False but no attested_span -> the fact claims attestation it can't back.
    intent = _intent(entity_resolutions=[_resolution()], facts=[_fact(attested_span=None)])
    v = validate_intent(intent)
    assert any(x.code == "surface_fact_unanchored" and x.severity == "review" for x in v)


def test_inferred_fact_without_span_is_allowed():
    # An inferred fact legitimately has no surface; it gets capped + reviewed by
    # the arbiter, not rejected by the structural validator.
    intent = _intent(
        entity_resolutions=[_resolution()], facts=[_fact(inferred=True, attested_span=None)]
    )
    assert validate_intent(intent) == []


def test_bad_supersession_action_is_fatal():
    sp = SupersessionProposal(entity_ref="m1", predicate="employer", qualifier="", action="delete")
    intent = _intent(entity_resolutions=[_resolution()], supersession_proposals=[sp])
    assert any(x.code == "bad_supersession_action" for x in validate_intent(intent))


def test_merge_proposal_carries_two_refs():
    # Merge proposals never auto-enact; structurally they just carry the pair.
    mp = EntityPairProposal(entity_a_ref="m1", entity_b_ref="m2", rationale="same person")
    intent = _intent(merge_proposals=[mp])
    # No fatal violations from merge proposals alone.
    assert not has_fatal(validate_intent(intent))
