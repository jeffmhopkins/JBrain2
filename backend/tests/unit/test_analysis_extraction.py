"""Extraction parsing, the domain ratchet, and prompt assembly — all pure."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedTemporal,
    ExtractionError,
    dedup_facts,
    normalize_future_assertion,
    parse_datetime,
    parse_extraction,
    ratchet_domain,
    resolve_relative_date,
    temporals_consistent,
    validate_backward_temporal,
)
from jbrain.analysis.pipeline import local_anchor
from jbrain.analysis.prompt import (
    EXTRACTION_SCHEMA,
    MAX_FACTS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)


def valid_payload() -> dict[str, Any]:
    return {
        "title": "Checkup at Dr. Patel",
        "tags": ["Health", "checkup", "blood pressure", "health"],
        "mentions": [
            {"name": "Me", "kind": "Person", "surface_text": "my"},
            {"name": "Dr. Patel", "kind": "Person", "surface_text": "Dr. Patel"},
        ],
        "facts": [
            {
                "predicate": "bloodPressure",
                "qualifier": "",
                "kind": "measurement",
                "statement": "Blood pressure was 118/76 this morning.",
                "value_json": {"systolic": 118, "diastolic": 76, "unit": "mmHg"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "this morning",
                    "resolved_start": "2026-06-10T08:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "health",
                "confidence": 0.95,
            }
        ],
        "temporal_tokens": [
            {
                "phrase": "this morning",
                "kind": "point",
                "resolved_start": "2026-06-10T08:00:00+00:00",
                "resolved_end": None,
                "precision": "day",
                "rrule": None,
            }
        ],
    }


def test_parse_valid_payload() -> None:
    parsed = parse_extraction(valid_payload())
    assert parsed.title == "Checkup at Dr. Patel"
    # Tags are lowercased and deduplicated.
    assert parsed.tags == ["health", "checkup", "blood pressure"]
    assert [m.name for m in parsed.mentions] == ["Me", "Dr. Patel"]
    fact = parsed.facts[0]
    assert fact.kind == "measurement"
    assert fact.temporal is not None
    assert fact.temporal.resolved_start == datetime(2026, 6, 10, 8, tzinfo=UTC)
    token = parsed.tokens[0]
    assert token.phrase == "this morning" and token.kind == "point"


def test_tags_capped_at_six() -> None:
    payload = valid_payload()
    payload["tags"] = [f"tag-{i}" for i in range(10)]
    assert len(parse_extraction(payload).tags) == 6


@pytest.mark.parametrize("missing", ["title", "tags", "mentions", "facts"])
def test_missing_top_level_field_raises(missing: str) -> None:
    payload = valid_payload()
    del payload[missing]
    with pytest.raises(ExtractionError, match=missing):
        parse_extraction(payload)


def test_non_object_payload_raises() -> None:
    with pytest.raises(ExtractionError):
        parse_extraction(["not", "an", "object"])
    with pytest.raises(ExtractionError):
        parse_extraction(None)


def test_invalid_fact_is_dropped_not_fatal() -> None:
    payload = valid_payload()
    payload["facts"].append({"predicate": "x", "kind": "vibe", "statement": "?"})
    payload["facts"].append("not even an object")
    parsed = parse_extraction(payload)
    assert len(parsed.facts) == 1  # only the valid one survives


def _raw_fact(i: int) -> dict[str, Any]:
    fact = dict(valid_payload()["facts"][0])
    fact["qualifier"] = f"q{i}"  # distinct identity keys, like a real over-extraction
    return fact


def test_facts_hard_capped_at_max_facts_keeping_first() -> None:
    """H1: the prompt's soft cap has server-side teeth; order is the model's
    salience ranking, so the FIRST N survive."""
    payload = valid_payload()
    payload["facts"] = [_raw_fact(i) for i in range(MAX_FACTS + 18)]
    parsed = parse_extraction(payload)
    assert len(parsed.facts) == MAX_FACTS
    assert [f.qualifier for f in parsed.facts] == [f"q{i}" for i in range(MAX_FACTS)]


def test_oversized_identity_key_rejects_the_fact() -> None:
    """predicate/qualifier are identity-key parts: truncation could collide
    two distinct keys, so the fact is dropped instead."""
    payload = valid_payload()
    payload["facts"][0]["predicate"] = "p" * 201
    assert parse_extraction(payload).facts == []
    payload = valid_payload()
    payload["facts"][0]["qualifier"] = "q" * 201
    assert parse_extraction(payload).facts == []


def test_oversized_statement_truncates_but_keeps_the_fact() -> None:
    payload = valid_payload()
    payload["facts"][0]["statement"] = "s" * 5000
    parsed = parse_extraction(payload)
    assert len(parsed.facts) == 1
    assert len(parsed.facts[0].statement) == 1000


def test_oversized_value_json_is_dropped_fact_survives() -> None:
    payload = valid_payload()
    payload["facts"][0]["value_json"] = {"blob": "x" * 20000}
    parsed = parse_extraction(payload)
    assert len(parsed.facts) == 1
    assert parsed.facts[0].value_json is None


def test_reasonable_value_json_passes_untouched() -> None:
    parsed = parse_extraction(valid_payload())
    assert parsed.facts[0].value_json == {"systolic": 118, "diastolic": 76, "unit": "mmHg"}


def test_unknown_domain_falls_back_to_empty_for_pipeline_substitution() -> None:
    payload = valid_payload()
    payload["facts"][0]["domain"] = "work"  # invented code: never trusted
    assert parse_extraction(payload).facts[0].domain == ""


def test_unresolved_temporal_token_is_dropped() -> None:
    """Never store only-relative (docs/ANALYSIS.md "Temporal model")."""
    payload = valid_payload()
    payload["temporal_tokens"].append(
        {
            "phrase": "back in the day",
            "kind": "point",
            "resolved_start": None,
            "resolved_end": None,
            "precision": "era",
            "rrule": None,
        }
    )
    assert len(parse_extraction(payload).tokens) == 1


def test_confidence_clamped() -> None:
    payload = valid_payload()
    payload["facts"][0]["confidence"] = 7
    assert parse_extraction(payload).facts[0].confidence == 1.0
    payload["facts"][0]["confidence"] = "garbage"
    assert parse_extraction(payload).facts[0].confidence == 0.0


def test_parse_datetime_handles_z_offsets_and_naive() -> None:
    assert parse_datetime("2026-06-10T08:00:00Z") == datetime(2026, 6, 10, 8, tzinfo=UTC)
    offset = parse_datetime("2026-06-10T08:00:00-05:00")
    assert offset is not None and offset.utcoffset() is not None
    naive = parse_datetime("2026-06-10T08:00:00")
    assert naive is not None and naive.tzinfo is UTC
    assert parse_datetime("soonish") is None
    assert parse_datetime(None) is None


# --- domain ratchet ---------------------------------------------------------


def test_ratchet_up_into_restricted_is_free() -> None:
    assert ratchet_domain("health", "general") == ("health", False)
    assert ratchet_domain("finance", "general") == ("finance", False)


def test_ratchet_never_relaxes_without_review() -> None:
    # A health note's fact claiming to be general keeps health + review.
    assert ratchet_domain("general", "health") == ("health", True)


def test_ratchet_cross_restricted_keeps_note_domain_with_review() -> None:
    assert ratchet_domain("finance", "health") == ("health", True)


def test_ratchet_same_domain_is_identity() -> None:
    assert ratchet_domain("general", "general") == ("general", False)
    assert ratchet_domain("health", "health") == ("health", False)


# --- prompt assembly --------------------------------------------------------


def test_system_prompt_carries_the_fact_grammar() -> None:
    for needle in (
        "schema.org",
        "event",
        "measurement",
        "state",
        "attribute",
        "preference",
        "relationship",
        "asserted",
        "hypothetical",
        "expected",
        '"Me"',
        "capture anchor",
        str(MAX_FACTS),
        "confidence",
    ):
        assert needle in SYSTEM_PROMPT, needle


def test_system_prompt_v2_teaches_kind_discipline_and_schema_org_names() -> None:
    """Bug 1/3: the prompt must steer relocation/residence/employer to a
    `state` on canonical schema.org predicates, reuse predicate names across
    notes, and type future follow-ups as `expected`."""
    # Canonical predicate vocabulary the model must converge on.
    for canonical in ("homeLocation", "worksFor", "address", "spouse", "birthDate"):
        assert canonical in SYSTEM_PROMPT, canonical
    # Kind-selection rules: residence/employer/address/marital are states.
    assert "state change" in SYSTEM_PROMPT
    for concept in ("residence", "employer", "address", "marital"):
        assert concept in SYSTEM_PROMPT, concept
    # The worked relocation example renders Denver -> homeLocation state.
    assert "homeLocation" in SYSTEM_PROMPT and "Denver" in SYSTEM_PROMPT
    assert "Boulder" in SYSTEM_PROMPT  # supersession by matching predicate
    # Future follow-ups are expected, not asserted events.
    assert "in 3 months" in SYSTEM_PROMPT and "expected" in SYSTEM_PROMPT


def test_system_prompt_v3_teaches_possessive_decomposition_and_no_normalizing() -> None:
    """Field gap: 'Summer's rat's name is Ricky' came back as ONE owner edge
    (no name attribute on Ricky, kind 'pet'), and a later 'the rat' mention
    was normalized to the invented name 'Rat'. v3 must teach the two-fact
    decomposition, species kinds, and that reference phrases stay verbatim."""
    # Possessive introductions decompose into owns edge + name attribute.
    assert "DECOMPOSE" in SYSTEM_PROMPT
    assert "X.owns -> N" in SYSTEM_PROMPT and "Me.owns -> N" in SYSTEM_PROMPT
    assert '"species"' in SYSTEM_PROMPT
    # Reference mentions are never normalized to invented proper names.
    assert "Never normalize a reference mention" in SYSTEM_PROMPT
    assert "resolver owns identity" in SYSTEM_PROMPT
    # Animal kinds are the species or Animal, never the useless "pet".
    assert 'never "pet"' in SYSTEM_PROMPT
    # The worked dog/rat example renders the exact field case.
    for needle in ("Bella", "Ricky", '"species": "dog"', '"species": "rat"', "FOUR facts"):
        assert needle in SYSTEM_PROMPT, needle
    assert '"the rat"' in SYSTEM_PROMPT


def test_system_prompt_v4_forbids_same_predicate_restatement() -> None:
    """Field gap: one note ('Jeff is ... born March 19, 1986, is 6\\'4" 255lb')
    came back with height THREE times (two attribute renderings + a
    measurement) and birthDate twice at different precisions. v4 must demand
    one fact per entity+predicate per note, normalized units, and a single
    kind choice per the kind table."""
    assert "ONE fact per entity+predicate per note" in SYSTEM_PROMPT
    assert "renderings, units, or kinds" in SYSTEM_PROMPT
    # The exact field case is the worked normalization example.
    assert '{"value": 76, "unit": "in"}' in SYSTEM_PROMPT
    # Kind chosen once: adult height/birthDate attribute, readings measurement.
    assert "never also a `measurement`" in SYSTEM_PROMPT
    assert "READING is a `measurement`" in SYSTEM_PROMPT


def test_system_prompt_v5_teaches_object_person_and_backward_temporal() -> None:
    """Field gaps (Jun 2026): 'Jeff is married to Celine Hopkins' dropped Celine
    entirely (object-of-relation), 'Jeff ate Celine's dinner' kept Celine only
    as a tag, and 'last night' resolved to the capture day. v5 must teach that a
    person in ANY grammatical role is a mention, that a relationship's object
    MUST also be a mention, and how backward phrases count from the local day."""
    # A non-subject person is still a mention — object, possessor, appositive.
    assert "NO MATTER THEIR GRAMMATICAL ROLE" in SYSTEM_PROMPT
    for needle in ("POSSESSOR", "appositive", "including in a tag"):
        assert needle in SYSTEM_PROMPT, needle
    # The relationship object must appear in mentions, not just the statement.
    assert 'MUST also appear in "mentions"' in SYSTEM_PROMPT
    assert "never drop the object" in SYSTEM_PROMPT
    # The worked Person<->Person example is the exact marriage field case.
    for needle in (
        '"object_entity_ref": "Celine Hopkins"',
        "only appears as a possessor",
    ):
        assert needle in SYSTEM_PROMPT, needle
    # Backward temporal: "last night" from a morning capture is the prior day.
    assert "last night" in SYSTEM_PROMPT and "PRIOR calendar day" in SYSTEM_PROMPT


def test_prompt_version_bumped_to_v9() -> None:
    assert PROMPT_VERSION == "note-extract-v9"


def test_system_prompt_v9_teaches_inanimate_ownership_edges() -> None:
    """Field gap (Jun 2026): "My truck is a white f150 from 2005" produced a lone
    Me.owns fact whose object was buried in the statement sentence, while the
    truck's attributes (color/year/engine) had no entity to hang off. v9 must
    teach that an owned inanimate thing is its own entity, the ownership is an
    object_entity_ref edge, and the description lives on the THING — not folded
    into the owns statement."""
    assert "An owned INANIMATE thing" in SYSTEM_PROMPT
    assert "object_entity_ref the Thing's mention" in SYSTEM_PROMPT
    # The worked vehicle example is the exact field case, edge + on-thing props.
    assert '"object_entity_ref": "my truck"' in SYSTEM_PROMPT
    assert 'entity_ref is "my truck"' in SYSTEM_PROMPT


def test_system_prompt_v9_links_place_and_org_valued_states_to_their_node() -> None:
    """Follow-up sweep: a state/relationship whose value IS a named entity must
    point AT that node, not bury it in a string. homeLocation links to the Place
    (a functional predicate, so the object stays out of its key and Boulder still
    supersedes Denver), worksFor to the Organization, and organization membership
    is memberOf so the org gets a reciprocal member edge. An address stays a
    value string — a full street address is not a place node."""
    assert '"object_entity_ref": "Denver"' in SYSTEM_PROMPT
    assert "set object_entity_ref to the Place mention" in SYSTEM_PROMPT
    assert "memberOf" in SYSTEM_PROMPT
    assert "object_entity_ref the Organization mention" in SYSTEM_PROMPT
    assert "a full street address is not a place node" in SYSTEM_PROMPT


def test_user_prompt_carries_anchor_with_timezone_domain_and_content() -> None:
    anchor = datetime(2026, 6, 10, 9, 30, tzinfo=UTC)
    prompt = build_user_prompt(["BP was 118/76", "second chunk"], anchor=anchor, domain="health")
    assert "2026-06-10T09:30:00+00:00" in prompt
    assert "health" in prompt
    assert "BP was 118/76" in prompt and "second chunk" in prompt


def test_user_prompt_appends_domain_block_for_sensitive_domains() -> None:
    # v6: health/finance notes get an entity-shape block (baseline showed
    # meds/conditions/accounts captured only as fact values, not mentions);
    # general/location get none.
    health = build_user_prompt(
        ["BP 120/80, lisinopril"], anchor=datetime(2026, 6, 10, tzinfo=UTC), domain="health"
    )
    assert "MEDICATION" in health and "linkable entity" in health
    # v7: a named clinician becomes a patient -> provider treatedBy edge, not
    # only a mention (the live eval showed providers never wired into a fact).
    assert "treatedBy" in health and "CLINICIAN" in health
    finance = build_user_prompt(
        ["paid rent, 401k"], anchor=datetime(2026, 6, 10, tzinfo=UTC), domain="finance"
    )
    assert "FINANCIAL INSTITUTION" in finance and "FUND" in finance
    general = build_user_prompt(
        ["went for a run"], anchor=datetime(2026, 6, 10, tzinfo=UTC), domain="general"
    )
    assert "MEDICATION" not in general and "FINANCIAL INSTITUTION" not in general


def test_user_prompt_anchor_carries_local_date_not_utc() -> None:
    """Bug 2: an evening-local capture whose UTC instant is the next calendar
    day must reach the model as its LOCAL date, or "today" drifts a day."""
    # 2026-06-10 17:11 at UTC-07:00 == 2026-06-11 00:11 UTC.
    local = datetime(2026, 6, 10, 17, 11, tzinfo=timezone(timedelta(hours=-7)))
    prompt = build_user_prompt(["note"], anchor=local, domain="general")
    assert "2026-06-10T17:11:00-07:00" in prompt
    assert "2026-06-11" not in prompt  # never the UTC-rolled date


# --- capture anchor locality (Bug 2) ----------------------------------------


def test_local_anchor_reprojects_utc_instant_into_client_offset() -> None:
    # Stored instant is UTC (timestamptz round-trip); the client wrote it at
    # 17:11 local, UTC-07:00, which is 00:11 the NEXT day in UTC.
    stored = datetime(2026, 6, 11, 0, 11, tzinfo=UTC)
    anchor = local_anchor(stored, -420)  # -7h in minutes
    assert anchor.isoformat() == "2026-06-10T17:11:00-07:00"
    assert anchor.date() == datetime(2026, 6, 10).date()


def test_local_anchor_without_offset_falls_back_to_stored_instant() -> None:
    stored = datetime(2026, 6, 11, 0, 11, tzinfo=UTC)
    assert local_anchor(stored, None) == stored


# --- future-tense assertion (Bug 3) -----------------------------------------


def _fact(assertion: str, start: datetime | None, kind: str = "event") -> ExtractedFact:
    temporal = (
        ExtractedTemporal(phrase="later", resolved_start=start, resolved_end=None, precision="day")
        if start is not None
        else None
    )
    return ExtractedFact(
        predicate="followUp",
        qualifier="",
        kind=kind,
        statement="Follow-up visit.",
        value_json=None,
        assertion=assertion,
        entity_ref="Me",
        object_entity_ref=None,
        temporal=temporal,
        domain="health",
        confidence=0.9,
    )


def test_future_asserted_fact_becomes_expected() -> None:
    anchor = datetime(2026, 6, 10, 17, 11, tzinfo=UTC)
    future = anchor + timedelta(days=92)  # "back in 3 months"
    assert normalize_future_assertion(_fact("asserted", future), anchor).assertion == "expected"


def test_past_and_present_facts_keep_their_assertion() -> None:
    anchor = datetime(2026, 6, 10, 17, 11, tzinfo=UTC)
    past = anchor - timedelta(days=1)
    assert normalize_future_assertion(_fact("asserted", past), anchor).assertion == "asserted"
    # No temporal: nothing to relax.
    assert normalize_future_assertion(_fact("asserted", None), anchor).assertion == "asserted"


def test_future_non_asserted_assertion_is_left_alone() -> None:
    anchor = datetime(2026, 6, 10, 17, 11, tzinfo=UTC)
    future = anchor + timedelta(days=30)
    # A future hypothetical stays hypothetical; we only relax bare asserted.
    assert normalize_future_assertion(_fact("hypothetical", future), anchor).assertion == (
        "hypothetical"
    )


def test_schema_and_version_are_stable_contract_surface() -> None:
    assert PROMPT_VERSION  # stamped on every fact
    assert set(EXTRACTION_SCHEMA["required"]) == {
        "title",
        "tags",
        "mentions",
        "facts",
        "temporal_tokens",
    }
    fact_schema = EXTRACTION_SCHEMA["properties"]["facts"]["items"]
    assert "temporal" in fact_schema["properties"]
    assert fact_schema["properties"]["domain"]["enum"] == [
        "general",
        "health",
        "finance",
        "location",
    ]


# --- same-key dedup within one extraction (field: triple height) ------------


def _dup(
    predicate: str,
    kind: str,
    value_json: dict[str, Any] | None,
    *,
    confidence: float = 0.9,
    statement: str = "stmt",
    temporal: ExtractedTemporal | None = None,
    qualifier: str = "",
    object_ref: str | None = None,
    assertion: str = "asserted",
) -> ExtractedFact:
    return ExtractedFact(
        predicate=predicate,
        qualifier=qualifier,
        kind=kind,
        statement=statement,
        value_json=value_json,
        assertion=assertion,
        entity_ref="Jeff",
        object_entity_ref=object_ref,
        temporal=temporal,
        domain="general",
        confidence=confidence,
    )


def _temporal(start: str, precision: str) -> ExtractedTemporal:
    return ExtractedTemporal(
        phrase=None,
        resolved_start=datetime.fromisoformat(start),
        resolved_end=None,
        precision=precision,
    )


def test_dedup_collapses_field_height_triple_to_best_confidence() -> None:
    """The exact field shape: height as a valued attribute, a valueless
    rendering, and a measurement — ONE fact survives, the most confident."""
    deduped = dedup_facts(
        [
            _dup("height", "attribute", {"value": 76, "unit": "in"}, confidence=0.92),
            _dup("height", "attribute", None, confidence=0.85, statement="Jeff is 6'4\" tall."),
            _dup(
                "height",
                "measurement",
                {"value": 76, "unit": "in"},
                confidence=0.8,
                temporal=_temporal("2026-06-10T23:02:00-06:00", "instant"),
            ),
        ]
    )
    assert len(deduped) == 1
    assert deduped[0].kind == "attribute" and deduped[0].confidence == 0.92


def test_dedup_recognizes_unit_converted_duplicate() -> None:
    # 76 in == 193.04 cm: the supersession conversion table must agree.
    deduped = dedup_facts(
        [
            _dup("height", "attribute", {"value": 76, "unit": "in"}, confidence=0.95),
            _dup("height", "attribute", {"value": 193, "unit": "cm"}, confidence=0.7),
        ]
    )
    assert len(deduped) == 1
    assert deduped[0].value_json == {"value": 76, "unit": "in"}


def test_dedup_precision_variants_keep_the_precise_date_despite_confidence() -> None:
    """'March 1986' and 'March 19, 1986' are the SAME date at differing
    precision: the precise one wins even at lower confidence — information
    beats a score on a vaguer rendering."""
    month = _dup(
        "birthDate",
        "attribute",
        {"date": "1986-03"},
        confidence=0.95,
        temporal=_temporal("1986-03-01T00:00:00+00:00", "month"),
    )
    day = _dup(
        "birthDate",
        "attribute",
        {"date": "1986-03-19"},
        confidence=0.9,
        temporal=_temporal("1986-03-19T00:00:00+00:00", "day"),
    )
    deduped = dedup_facts([month, day])
    assert len(deduped) == 1
    assert deduped[0].value_json == {"date": "1986-03-19"}
    # Order must not matter: vaguer-after-preciser also keeps the precise one.
    deduped = dedup_facts([day, month])
    assert len(deduped) == 1 and deduped[0].value_json == {"date": "1986-03-19"}


def test_dedup_inconsistent_dates_fall_through_to_contradiction() -> None:
    # April 1986 does NOT contain March 19, 1986: a real disagreement, kept
    # for the attribute-collision machinery.
    facts = [
        _dup(
            "birthDate",
            "attribute",
            {"date": "1986-04"},
            temporal=_temporal("1986-04-01T00:00:00+00:00", "month"),
        ),
        _dup(
            "birthDate",
            "attribute",
            {"date": "1986-03-19"},
            temporal=_temporal("1986-03-19T00:00:00+00:00", "day"),
        ),
    ]
    assert len(dedup_facts(facts)) == 2


def test_dedup_keeps_genuinely_different_values_for_conflict_machinery() -> None:
    """adv_self_contradiction_one_note semantics survive: same key, different
    VALUES (Reykjavik vs Oslo) is a contradiction, never silently collapsed."""
    facts = [
        _dup("homeLocation", "state", {"city": "Reykjavik"}),
        _dup("homeLocation", "state", {"city": "Oslo"}),
    ]
    assert len(dedup_facts(facts)) == 2


def test_dedup_key_separates_objects_and_assertions() -> None:
    # Distinct edges accumulate; a negation never restates its assertion.
    facts = [
        _dup("owns", "relationship", None, object_ref="Bella"),
        _dup("owns", "relationship", None, object_ref="Ricky"),
        _dup("owns", "relationship", None, object_ref="Bella", assertion="negated"),
    ]
    assert len(dedup_facts(facts)) == 3


def test_dedup_ties_prefer_valued_then_later() -> None:
    valued = _dup("height", "attribute", {"value": 76, "unit": "in"})
    bare = _dup("height", "attribute", None, statement="second rendering")
    assert dedup_facts([bare, valued]) == [valued]
    # Equal confidence, both valueless: later in array wins (last-wins).
    first = _dup("nickname", "attribute", None, statement="first")
    second = _dup("nickname", "attribute", None, statement="second")
    assert dedup_facts([first, second]) == [second]


def test_parse_extraction_applies_the_dedup_guard() -> None:
    payload = valid_payload()
    payload["facts"] = [dict(payload["facts"][0]), dict(payload["facts"][0])]
    payload["facts"][1]["kind"] = "state"  # same key, re-kinded restatement
    assert len(parse_extraction(payload).facts) == 1


# --- temporal consistency (the duplicate-vs-contradiction line) --------------


def test_temporals_consistent_vacuous_and_identical() -> None:
    t = _temporal("1986-03-19T00:00:00+00:00", "day")
    assert temporals_consistent(None, t)
    assert temporals_consistent(t, None)
    assert temporals_consistent(t, _temporal("1986-03-19T00:00:00+00:00", "day"))


def test_temporals_consistent_truncates_to_the_vaguer_precision() -> None:
    month = _temporal("1986-03-01T00:00:00+00:00", "month")
    day = _temporal("1986-03-19T00:00:00+00:00", "day")
    year = _temporal("1986-01-01T00:00:00+00:00", "year")
    assert temporals_consistent(month, day)
    assert temporals_consistent(year, day)
    assert not temporals_consistent(_temporal("1986-04-01T00:00:00+00:00", "month"), day)
    assert not temporals_consistent(_temporal("1987-01-01T00:00:00+00:00", "year"), day)


def test_temporals_consistent_never_assumes_for_era_unknown_or_instants() -> None:
    day = _temporal("1986-03-19T00:00:00+00:00", "day")
    assert not temporals_consistent(_temporal("1986-03-19T00:00:00+00:00", "era"), day)
    assert not temporals_consistent(_temporal("1986-03-19T00:00:00+00:00", "unknown"), day)
    # Two distinct instants are distinct readings, not duplicates.
    assert not temporals_consistent(
        _temporal("2026-06-10T08:00:00+00:00", "instant"),
        _temporal("2026-06-10T20:00:00+00:00", "instant"),
    )


# --- Increment 1: backward relative-phrase resolution repair ----------------

_MST = timezone(timedelta(hours=-6))
# The owner field report: a 07:13 capture where "last night" belongs to the
# PRIOR day, but the model resolved it to the capture day.
_ANCHOR = datetime(2026, 6, 11, 7, 13, tzinfo=_MST)


def test_resolve_relative_date_same_and_prior_day() -> None:
    assert resolve_relative_date("today", _ANCHOR) == _ANCHOR.date()
    assert resolve_relative_date("this morning", _ANCHOR) == _ANCHOR.date()
    assert resolve_relative_date("last night", _ANCHOR) == (_ANCHOR - timedelta(days=1)).date()
    assert resolve_relative_date("Yesterday.", _ANCHOR) == (_ANCHOR - timedelta(days=1)).date()
    assert (
        resolve_relative_date("day before yesterday", _ANCHOR)
        == (_ANCHOR - timedelta(days=2)).date()
    )


def test_resolve_relative_date_counted_offsets() -> None:
    assert resolve_relative_date("3 days ago", _ANCHOR) == (_ANCHOR - timedelta(days=3)).date()
    assert resolve_relative_date("a day ago", _ANCHOR) == (_ANCHOR - timedelta(days=1)).date()
    assert resolve_relative_date("two weeks ago", _ANCHOR) == (_ANCHOR - timedelta(weeks=2)).date()


def test_resolve_relative_date_last_weekday_is_strictly_prior() -> None:
    assert _ANCHOR.weekday() == 3  # Thursday
    # "last Thursday" is a week back, never the anchor's own day.
    assert resolve_relative_date("last Thursday", _ANCHOR) == (_ANCHOR - timedelta(days=7)).date()
    assert resolve_relative_date("last Tuesday", _ANCHOR) == (_ANCHOR - timedelta(days=2)).date()


def test_resolve_relative_date_unknown_or_ambiguous_is_none() -> None:
    # "last week" is a range, not a day — left to the model, never guessed.
    assert resolve_relative_date("last week", _ANCHOR) is None
    assert resolve_relative_date("around the holidays", _ANCHOR) is None
    assert resolve_relative_date(None, _ANCHOR) is None
    assert resolve_relative_date("", _ANCHOR) is None


def test_validate_backward_temporal_repairs_off_by_one() -> None:
    wrong = ExtractedTemporal(
        phrase="last night",
        resolved_start=datetime(2026, 6, 11, 22, 0, tzinfo=_MST),  # capture day: wrong
        resolved_end=None,
        precision="day",
    )
    fixed, repaired = validate_backward_temporal(wrong, _ANCHOR)
    assert repaired and fixed is not None and fixed.resolved_start is not None
    assert fixed.resolved_start.date() == date(2026, 6, 10)
    # Only the calendar date shifts; time-of-day and offset are preserved.
    assert fixed.resolved_start.hour == 22
    assert fixed.resolved_start.utcoffset() == timedelta(hours=-6)


def test_validate_backward_temporal_preserves_range_width() -> None:
    wrong = ExtractedTemporal(
        phrase="yesterday",
        resolved_start=datetime(2026, 6, 11, 18, 0, tzinfo=_MST),
        resolved_end=datetime(2026, 6, 11, 23, 0, tzinfo=_MST),
        precision="day",
    )
    fixed, repaired = validate_backward_temporal(wrong, _ANCHOR)
    assert repaired and fixed is not None
    assert fixed.resolved_start is not None and fixed.resolved_end is not None
    assert fixed.resolved_start.date() == date(2026, 6, 10)
    assert fixed.resolved_end.date() == date(2026, 6, 10)
    assert fixed.resolved_end - fixed.resolved_start == timedelta(hours=5)


def test_validate_backward_temporal_noop_when_already_correct() -> None:
    right = ExtractedTemporal(
        phrase="last night",
        resolved_start=datetime(2026, 6, 10, 22, 0, tzinfo=_MST),
        resolved_end=None,
        precision="day",
    )
    fixed, repaired = validate_backward_temporal(right, _ANCHOR)
    assert not repaired and fixed is right


def test_validate_backward_temporal_leaves_ambiguous_phrases_alone() -> None:
    ambiguous = ExtractedTemporal(
        phrase="last week",
        resolved_start=datetime(2026, 6, 11, 12, 0, tzinfo=_MST),
        resolved_end=None,
        precision="day",
    )
    fixed, repaired = validate_backward_temporal(ambiguous, _ANCHOR)
    assert not repaired and fixed is ambiguous


def test_resolve_relative_date_last_night_is_ambiguous_at_evening_anchor() -> None:
    # From a late-evening capture "last night" can mean earlier the SAME night,
    # so we don't guess; a daytime capture is unambiguous.
    evening = datetime(2026, 6, 11, 23, 45, tzinfo=_MST)
    assert resolve_relative_date("last night", evening) is None
    assert resolve_relative_date("last night", _ANCHOR) == (_ANCHOR - timedelta(days=1)).date()


def test_validate_backward_temporal_repairs_utc_model_output_by_local_day() -> None:
    # grok routinely resolves to a UTC instant instead of echoing the note's
    # local offset. The repair must still judge it by the LOCAL calendar day:
    # 2026-06-11T20:00Z is Jun 11 14:00 at -06:00 (the capture DAY) — wrong for
    # "last night" from a 07:13 anchor, so it shifts to Jun 10 (the live bug).
    utc_start = ExtractedTemporal(
        phrase="last night",
        resolved_start=datetime(2026, 6, 11, 20, 0, tzinfo=UTC),
        resolved_end=None,
        precision="day",
    )
    fixed, repaired = validate_backward_temporal(utc_start, _ANCHOR)
    assert repaired and fixed is not None and fixed.resolved_start is not None
    assert fixed.resolved_start.astimezone(_MST).date() == date(2026, 6, 10)


def test_validate_backward_temporal_noop_when_utc_output_is_locally_correct() -> None:
    # 2026-06-11T02:00Z is Jun 10 20:00 at -06:00 — already the right local day
    # for "last night", so no shift even though the offset differs from anchor's.
    utc_start = ExtractedTemporal(
        phrase="last night",
        resolved_start=datetime(2026, 6, 11, 2, 0, tzinfo=UTC),
        resolved_end=None,
        precision="day",
    )
    fixed, repaired = validate_backward_temporal(utc_start, _ANCHOR)
    assert not repaired and fixed is utc_start


def _last_night_payload() -> dict[str, Any]:
    """A note whose 'last night' the model wrongly resolved to the capture day."""
    return {
        "title": "Dinner",
        "tags": ["dinner", "food", "celine"],
        "mentions": [{"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}],
        "facts": [
            {
                "predicate": "ate", "qualifier": "", "kind": "event",
                "statement": "Jeff ate dinner last night.", "value_json": None,
                "assertion": "asserted", "entity_ref": "Jeff", "object_entity_ref": None,
                "temporal": {
                    "phrase": "last night",
                    "resolved_start": "2026-06-11T20:00:00-06:00",
                    "resolved_end": None, "precision": "day",
                },
                "domain": "general", "confidence": 0.7,
            }
        ],
        "temporal_tokens": [
            {
                "phrase": "last night", "kind": "point",
                "resolved_start": "2026-06-11T20:00:00-06:00",
                "resolved_end": None, "precision": "day", "rrule": None,
            }
        ],
    }  # fmt: skip


def test_parse_extraction_repairs_fact_and_token_when_anchored() -> None:
    parsed = parse_extraction(_last_night_payload(), anchor=_ANCHOR)
    fact_t = parsed.facts[0].temporal
    assert fact_t is not None and fact_t.resolved_start is not None
    assert fact_t.resolved_start.date() == date(2026, 6, 10)
    assert parsed.tokens[0].resolved_start.date() == date(2026, 6, 10)


def test_parse_extraction_without_anchor_leaves_resolution_raw() -> None:
    # Back-compat: callers that omit the anchor (most unit tests) see the
    # model's value untouched.
    parsed = parse_extraction(_last_night_payload())
    fact_t = parsed.facts[0].temporal
    assert fact_t is not None and fact_t.resolved_start is not None
    assert fact_t.resolved_start.date() == date(2026, 6, 11)
    assert parsed.tokens[0].resolved_start.date() == date(2026, 6, 11)


def test_finalize_temporal_stamps_absolute_date_to_local_midnight() -> None:
    # grok resolves "June 8" to midnight UTC; at -06:00 that instant is Jun 7
    # evening, so the local date drifts back one. Normalization re-stamps local
    # midnight on the written date, so the local date reads Jun 8 again.
    utc_midnight = ExtractedTemporal(
        phrase="June 8", resolved_start=datetime(2026, 6, 8, 0, 0, tzinfo=UTC),
        resolved_end=None, precision="day",
    )  # fmt: skip
    fixed, changed = validate_backward_temporal(utc_midnight, _ANCHOR)
    assert changed and fixed is not None and fixed.resolved_start is not None
    assert fixed.resolved_start.utcoffset() == timedelta(hours=-6)
    assert fixed.resolved_start.astimezone(_MST).date() == date(2026, 6, 8)
    # An instant-precision value (a measurement time) is left exactly as resolved.
    inst = ExtractedTemporal(
        phrase="", resolved_start=datetime(2026, 6, 8, 14, 0, tzinfo=UTC),
        resolved_end=None, precision="instant",
    )  # fmt: skip
    _, changed2 = validate_backward_temporal(inst, _ANCHOR)
    assert not changed2


def test_domain_floor_raises_general_to_restricted_only() -> None:
    from jbrain.analysis.extraction import domain_floor

    assert domain_floor("bloodPressure") == "health"
    assert domain_floor("medication") == "health"
    assert domain_floor("mortgage") == "finance"
    assert domain_floor("latitude") == "location"  # precise geo only
    assert domain_floor("ate") is None  # unknown -> model decides
    # Ambiguous / not-firewall-sensitive predicates are deliberately not floored.
    assert domain_floor("weight") is None and domain_floor("temperature") is None
    assert domain_floor("homeLocation") is None  # a home city is ordinary


def test_part_of_day_token_becomes_a_within_day_range() -> None:
    payload: dict[str, Any] = {
        "title": "t", "tags": ["a", "b", "c"],
        "mentions": [{"name": "Me", "kind": "Person", "surface_text": "I"}],
        "facts": [{
            "predicate": "ran", "qualifier": "", "kind": "event", "statement": "Ran this morning.",
            "value_json": None, "assertion": "asserted", "entity_ref": "Me",
            "object_entity_ref": None,
            "temporal": {"phrase": "this morning", "resolved_start": "2026-06-11T15:00:00+00:00",
                         "resolved_end": None, "precision": "day"},
            "domain": "general", "confidence": 0.9,
        }],
        "temporal_tokens": [{"phrase": "this morning", "kind": "point",
                             "resolved_start": "2026-06-11T15:00:00+00:00", "resolved_end": None,
                             "precision": "day", "rrule": None}],
    }  # fmt: skip
    parsed = parse_extraction(payload, anchor=_ANCHOR)  # _ANCHOR is 2026-06-11, -06:00
    tok = parsed.tokens[0]
    # 15:00 UTC == 09:00 local; the token keeps that start and gains the morning
    # window END (12:00), becoming a within-day range.
    assert tok.kind == "range" and tok.resolved_end is not None and tok.resolved_start is not None
    assert tok.resolved_start.astimezone(_MST).hour == 9
    assert tok.resolved_end.astimezone(_MST).hour == 12
    assert tok.resolved_start.astimezone(_MST).date() == date(2026, 6, 11)
    # The FACT is left untouched (token-only): valid_from keeps its time, no
    # valid_to (a state interval must never be falsely closed), and it shares the
    # token's start so there's no duplicate token.
    ft = parsed.facts[0].temporal
    assert ft is not None and ft.resolved_start is not None
    assert ft.resolved_start.astimezone(_MST).hour == 9 and ft.resolved_end is None


def test_relative_phrase_rendered_midnight_utc_is_not_pushed_a_day() -> None:
    # grok renders "yesterday" as midnight UTC: 2026-06-11T00:00Z is locally
    # Jun 10 at -06:00 (correct for an anchor of Jun 11). It must NOT be stamped
    # to the written UTC date (Jun 11) — a regression in the abs-date
    # normalization caught by the live finance eval.
    t = ExtractedTemporal(
        phrase="yesterday", resolved_start=datetime(2026, 6, 11, 0, 0, tzinfo=UTC),
        resolved_end=None, precision="day",
    )  # fmt: skip
    fixed, _ = validate_backward_temporal(t, _ANCHOR)  # _ANCHOR is Jun 11, -06:00
    assert fixed is not None and fixed.resolved_start is not None
    assert fixed.resolved_start.astimezone(_MST).date() == date(2026, 6, 10)
    # An ABSOLUTE date the model rendered as midnight UTC is still normalized.
    abs_t = ExtractedTemporal(
        phrase="June 8", resolved_start=datetime(2026, 6, 8, 0, 0, tzinfo=UTC),
        resolved_end=None, precision="day",
    )  # fmt: skip
    abs_fixed, _ = validate_backward_temporal(abs_t, _ANCHOR)
    assert abs_fixed is not None and abs_fixed.resolved_start is not None
    assert abs_fixed.resolved_start.astimezone(_MST).date() == date(2026, 6, 8)


def test_drift_predicates_normalize_to_canonical() -> None:
    # The registry's renamed_from attractor is applied during parse, so a note
    # that says "legalName" lands on the canonical name.legal address — the same
    # identity key a later "legal_name" note would, keeping one history.
    payload = {
        "title": "Names",
        "tags": [],
        "mentions": [{"name": "Me", "kind": "Person", "surface_text": "I"}],
        "facts": [
            {
                "predicate": "legalName",
                "qualifier": "",
                "kind": "state",
                "statement": "My legal name is Jeffrey Mark Hopkins.",
                "value_json": None,
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": None,
                "domain": "general",
                "confidence": 1.0,
            }
        ],
        "temporal_tokens": [],
    }
    assert [f.predicate for f in parse_extraction(payload).facts] == ["name.legal"]
