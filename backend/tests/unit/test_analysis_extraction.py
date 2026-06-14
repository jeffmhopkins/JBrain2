"""Extraction parsing, the domain ratchet, and prompt assembly — all pure."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedMention,
    ExtractedTemporal,
    ExtractedToken,
    Extraction,
    ExtractionError,
    dedup_facts,
    link_relationship_objects,
    merge_extractions,
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
    GROUP_CHAR_BUDGET,
    MAX_FACTS,
    MIN_FACTS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
    fact_cap,
    group_texts,
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
    salience ranking, so the FIRST N survive. Omitting max_facts defaults to the
    hard ceiling."""
    payload = valid_payload()
    payload["facts"] = [_raw_fact(i) for i in range(MAX_FACTS + 18)]
    parsed = parse_extraction(payload)
    assert len(parsed.facts) == MAX_FACTS
    assert [f.qualifier for f in parsed.facts] == [f"q{i}" for i in range(MAX_FACTS)]


def test_facts_capped_at_the_per_note_budget_passed_in() -> None:
    """A short note's smaller budget trims harder than the ceiling: the pipeline
    passes fact_cap(note), and the parser enforces exactly that number."""
    payload = valid_payload()
    payload["facts"] = [_raw_fact(i) for i in range(20)]
    parsed = parse_extraction(payload, max_facts=MIN_FACTS)
    assert len(parsed.facts) == MIN_FACTS
    assert [f.qualifier for f in parsed.facts] == [f"q{i}" for i in range(MIN_FACTS)]


def test_dropped_facts_reports_the_truncation_count() -> None:
    """The pipeline surfaces a hit budget as a review card, so the count of
    clipped tail facts must ride out on the Extraction."""
    payload = valid_payload()
    payload["facts"] = [_raw_fact(i) for i in range(MIN_FACTS + 5)]
    parsed = parse_extraction(payload, max_facts=MIN_FACTS)
    assert len(parsed.facts) == MIN_FACTS
    assert parsed.dropped_facts == 5


def test_dropped_facts_is_zero_within_budget() -> None:
    assert parse_extraction(valid_payload()).dropped_facts == 0


def test_fact_cap_scales_with_length_and_clamps() -> None:
    """The budget floors at MIN_FACTS for short notes, scales up with word count,
    and is bounded by the hard ceiling — so a long entry is no longer truncated
    at the old static 12 while a one-liner keeps a generous floor."""
    assert fact_cap("") == MIN_FACTS
    assert fact_cap(" ".join(["w"] * 30)) == MIN_FACTS  # dense short note: floor
    assert fact_cap(" ".join(["w"] * 90)) == MIN_FACTS  # still floored
    mid = fact_cap(" ".join(["w"] * 160))
    assert MIN_FACTS < mid < MAX_FACTS  # scales between the bounds
    assert fact_cap(" ".join(["w"] * 4000)) == MAX_FACTS  # bounded by the ceiling


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


def test_system_prompt_teaches_the_capture_contract() -> None:
    """v14 reframes extraction as the high-recall CAPTURE stage of a two-stage
    pipeline: capture everything stated, defer all judgment (identity,
    supersession, inference) to the integrator. The data/instruction boundary
    is load-bearing for prompt-injection resistance."""
    assert "CAPTURE stage of a two-stage" in SYSTEM_PROMPT
    assert "CAPTURE EVERYTHING THE NOTE STATES" in SYSTEM_PROMPT
    assert "CAPTURE ONLY WHAT THE NOTE STATES" in SYSTEM_PROMPT
    # Judgment is explicitly the integrator's job, not extraction's.
    assert "the integrator" in SYSTEM_PROMPT
    assert "Inference, identity, and supersession are the integrator's job." in SYSTEM_PROMPT
    assert "Do not infer unstated facts" in SYSTEM_PROMPT
    # Prompt-injection boundary.
    assert "DATA, NOT INSTRUCTIONS" in SYSTEM_PROMPT


def test_system_prompt_teaches_mentions_in_any_grammatical_role() -> None:
    """A person is a mention in ANY role (object, possessor, appositive), the
    author is "Me", and a reference phrase is kept verbatim — extraction never
    invents a proper name or guesses identity (the integrator owns that)."""
    assert "in ANY grammatical role" in SYSTEM_PROMPT
    for needle in (
        "OBJECT of a verb or preposition",
        "POSSESSOR",
        "appositive",
        "including in a tag",
    ):
        assert needle in SYSTEM_PROMPT, needle
    assert '"Me"' in SYSTEM_PROMPT
    # Reference mentions stay verbatim; no invented proper names.
    assert "never invent a proper name" in SYSTEM_PROMPT
    assert "The integrator owns identity" in SYSTEM_PROMPT
    # Animal kinds are the species, never the useless "pet".
    assert 'never "pet"' in SYSTEM_PROMPT


def test_system_prompt_teaches_the_fact_grammar() -> None:
    """The property-graph edge grammar: the six kinds, a relationship's object
    must also be a mention, enumerated relationships fan out one edge per person,
    and a measurement carries value+unit in value_json."""
    for kind in ("state", "event", "measurement", "attribute", "preference", "relationship"):
        assert kind in SYSTEM_PROMPT, kind
    # A relationship's object must be a real mention, not buried in the statement.
    assert "object_entity_ref to the OTHER party's mention name" in SYSTEM_PROMPT
    assert 'MUST also appear in "mentions"' in SYSTEM_PROMPT
    # Enumerated relationships fan out to one edge per person.
    assert "ONE edge PER person" in SYSTEM_PROMPT
    # Measurement value shape.
    assert '{"value": 178, "unit": "lb"}' in SYSTEM_PROMPT


def test_system_prompt_teaches_declared_names_and_aliases() -> None:
    """A declared name/alias must become its own name.* attribute carrying the
    bare string in value_json — the datum entity_aliases resolution depends on —
    never folded into a statement or the ownership edge. Possessive
    introductions decompose into owns edge + name attribute."""
    assert "A name or alias the note DECLARES" in SYSTEM_PROMPT
    for predicate in ("name.legal", "name.preferred", "name.nickname"):
        assert predicate in SYSTEM_PROMPT, predicate
    assert 'value_json {"value": "..."}' in SYSTEM_PROMPT
    assert "BOTH the owns edge AND the name attribute" in SYSTEM_PROMPT


def test_system_prompt_teaches_assertions_and_backward_temporal() -> None:
    """Assertion typing (future->expected, second-hand->reported, weighed
    possibility->hypothetical, denial->negated) and backward temporal: a
    relative phrase resolves against the anchor's LOCAL day, never invented."""
    assert "expected" in SYSTEM_PROMPT and 'is "reported"' in SYSTEM_PROMPT
    assert "hypothetical" in SYSTEM_PROMPT and "negated" in SYSTEM_PROMPT
    # Backward temporal: "last night" from a morning capture is the prior day.
    assert "last night" in SYSTEM_PROMPT and "PRIOR calendar day" in SYSTEM_PROMPT
    assert "Never invent a date" in SYSTEM_PROMPT


def test_system_prompt_teaches_per_fact_domain_for_the_firewall() -> None:
    """Domain is judged PER FACT regardless of the note's capture domain, so a
    family member's health fact in a general journal still floors to health —
    the firewall's input. When unsure, choose the sensitive domain."""
    assert "judged PER FACT" in SYSTEM_PROMPT
    assert "health even inside a general journal" in SYSTEM_PROMPT
    assert "choose the sensitive one" in SYSTEM_PROMPT


def test_user_prompt_carries_the_per_note_fact_budget() -> None:
    anchor = datetime(2026, 6, 10, 9, 30, tzinfo=UTC)
    prompt = build_user_prompt(["a note"], anchor=anchor, domain="general", max_facts=17)
    assert "Fact budget for this note: at most 17 facts" in prompt


def test_prompt_version_is_v14() -> None:
    assert PROMPT_VERSION == "note-extract-v14"


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


# ---- Deterministic relationship-object linking (link_relationship_objects) ----


def _rel(
    *,
    statement: str,
    object_ref: str | None,
    value_json: dict[str, Any] | None = None,
    entity_ref: str = "Me",
    kind: str = "relationship",
    predicate: str = "spouse",
) -> ExtractedFact:
    return ExtractedFact(
        predicate=predicate,
        qualifier="",
        kind=kind,
        statement=statement,
        value_json=value_json,
        assertion="asserted",
        entity_ref=entity_ref,
        object_entity_ref=object_ref,
        temporal=None,
        domain="general",
        confidence=0.9,
    )


def _person(name: str, surface: str | None = None) -> ExtractedMention:
    return ExtractedMention(name=name, kind="Person", surface_text=surface or name)


def test_link_snaps_a_near_miss_ref_to_its_mention() -> None:
    # The model named the object but case/possessive drifts off the mention.
    mentions = [_person("Me", "I"), _person("Celine")]
    fact = _rel(statement="Jeff owns Celine's bike.", object_ref="Celine's", predicate="owns")
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref == "Celine"


def test_link_recovers_dropped_ref_from_the_statement() -> None:
    # The exact report: spouse edge with the object folded into the sentence.
    mentions = [_person("Me", "I"), _person("Celine Hopkins")]
    fact = _rel(statement="I have a wife Celine Hopkins.", object_ref=None)
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref == "Celine Hopkins"


def test_link_recovers_dropped_ref_from_value_json() -> None:
    mentions = [_person("Me", "I"), _person("Celine Hopkins")]
    fact = _rel(statement="My spouse.", object_ref=None, value_json={"value": "Celine Hopkins"})
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref == "Celine Hopkins"


def test_link_leaves_ambiguous_statement_unlinked() -> None:
    # Two non-subject people named — never guess which one the edge points at.
    mentions = [_person("Me", "I"), _person("Celine"), _person("Sarah")]
    fact = _rel(statement="Celine and Sarah came over.", object_ref=None)
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref is None


def test_link_ignores_non_relationship_facts() -> None:
    # "Jeff ate Celine's dinner" is an event, not a relationship — the dropped
    # person is the separate entity-recall issue, not this net's job.
    mentions = [_person("Me", "I"), _person("Celine")]
    fact = _rel(
        statement="Jeff ate Celine's dinner.", object_ref=None, kind="event", predicate="ate"
    )
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref is None


def _org(name: str) -> ExtractedMention:
    return ExtractedMention(name=name, kind="Organization", surface_text=name)


def test_link_snaps_a_state_facts_near_miss_object() -> None:
    # worksFor/homeLocation are `state`, not `relationship`, yet still carry an
    # object — the snap re-points a case/possessive near-miss at its mention
    # regardless of kind (it never partial-matches "Umbrella" → "Umbrella Corp").
    mentions = [_person("Felix"), _org("Umbrella Corp")]
    fact = _rel(
        statement="Felix works for Umbrella Corp.",
        object_ref="umbrella corp",
        entity_ref="Felix",
        kind="state",
        predicate="worksFor",
    )
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref == "Umbrella Corp"


def test_link_does_not_infer_a_dropped_object_for_state_facts() -> None:
    # A state fact's missing object is left alone — value_json carries its value
    # and inferring a person/org from prose is the risk reserved for none.
    mentions = [_person("Felix"), _org("Umbrella Corp")]
    fact = _rel(
        statement="Felix works for Umbrella Corp.",
        object_ref=None,
        entity_ref="Felix",
        kind="state",
        predicate="worksFor",
    )
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref is None


def test_link_does_not_bind_the_subject_to_itself() -> None:
    # A self-naming statement must not recover its own subject as the object.
    mentions = [_person("Me", "I")]
    fact = _rel(statement="I am married.", object_ref=None)
    [linked] = link_relationship_objects([fact], mentions)
    assert linked.object_entity_ref is None


def test_parse_extraction_links_relationship_objects() -> None:
    # End to end through parse_extraction: the wired pass binds the edge so the
    # stored fact points at the object node instead of rendering its statement.
    payload = {
        "title": "About me",
        "tags": ["family", "marriage", "intro"],
        "mentions": [
            {"name": "Me", "kind": "Person", "surface_text": "I"},
            {"name": "Celine Hopkins", "kind": "Person", "surface_text": "Celine Hopkins"},
        ],
        "facts": [
            {
                "predicate": "spouse",
                "qualifier": "",
                "kind": "relationship",
                "statement": "I have a wife Celine Hopkins.",
                "value_json": None,
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": None,
                "domain": "general",
                "confidence": 0.95,
            }
        ],
        "temporal_tokens": [],
    }
    [fact] = parse_extraction(payload).facts
    assert fact.object_entity_ref == "Celine Hopkins"


# --- chunk-level map-reduce: grouping + merge -------------------------------


def test_group_texts_keeps_a_short_note_as_one_group() -> None:
    """The common path is unchanged: content under the budget is one group, so
    a short note still makes exactly one extraction call."""
    texts = ["a paragraph", "another short one"]
    assert group_texts(texts) == [texts]


def test_group_texts_fans_out_over_the_budget_without_splitting_blocks() -> None:
    """Long notes partition into ordered groups, each under budget; a block is
    never split (paragraph chunks are the atomic citation unit)."""
    half = "x" * (GROUP_CHAR_BUDGET // 2 + 100)  # two of these exceed one budget
    groups = group_texts([half, half, "tail"])
    assert groups == [[half], [half, "tail"]]
    # Every block survives intact and in order across the partition.
    assert [t for g in groups for t in g] == [half, half, "tail"]


def test_group_texts_isolates_a_lone_oversize_block() -> None:
    big = "y" * (GROUP_CHAR_BUDGET * 2)
    assert group_texts([big, "small"]) == [[big], ["small"]]


def _mr_part(
    *,
    title: str = "",
    tags: list[str] | None = None,
    mentions: list[ExtractedMention] | None = None,
    facts: list[ExtractedFact] | None = None,
    tokens: list[ExtractedToken] | None = None,
    dropped: int = 0,
) -> Extraction:
    return Extraction(
        title=title,
        tags=tags or [],
        mentions=mentions or [],
        facts=facts or [],
        tokens=tokens or [],
        dropped_facts=dropped,
    )


def _mr_rel(entity: str, obj: str | None, *, predicate: str = "spouse") -> ExtractedFact:
    return ExtractedFact(
        predicate=predicate,
        qualifier="",
        kind="relationship",
        statement=f"{entity}.{predicate} -> {obj}",
        value_json=None,
        assertion="asserted",
        entity_ref=entity,
        object_entity_ref=obj,
        temporal=None,
        domain="general",
        confidence=0.9,
    )


def _mr_person(name: str) -> ExtractedMention:
    return ExtractedMention(name=name, kind="Person", surface_text=name)


def test_merge_extractions_passes_a_single_part_through_untouched() -> None:
    part = _mr_part(title="T", facts=[_mr_rel("Me", "Bob")])
    assert merge_extractions([part]) is part


def test_merge_extractions_unions_metadata_and_sums_dropped() -> None:
    a = _mr_part(title="First", tags=["x", "y"], mentions=[_mr_person("Ann")], dropped=2)
    b = _mr_part(
        title="Second", tags=["y", "z"], mentions=[_mr_person("Ann"), _mr_person("Bob")], dropped=3
    )
    merged = merge_extractions([a, b])
    assert merged.title == "First"  # first non-empty wins
    assert merged.tags == ["x", "y", "z"]  # ordered union
    assert [m.name for m in merged.mentions] == ["Ann", "Bob"]  # deduped by name
    assert merged.dropped_facts == 5  # truncation summed for the note-level card


def test_merge_extractions_rebinds_a_relationship_object_named_in_another_group() -> None:
    """The cross-group win: a relationship whose object entity was mentioned in a
    DIFFERENT group still links, because the object binding re-runs over the
    full mention set. A per-group pass could not have snapped the possessive."""
    group1 = _mr_part(mentions=[_mr_person("Celine")])
    # The object ref is a possessive near-miss and Celine is not in THIS group's
    # mentions, so parse's per-group link could not bind it.
    group2 = _mr_part(mentions=[_mr_person("Jeff")], facts=[_mr_rel("Jeff", "Celine's")])
    [fact] = merge_extractions([group1, group2]).facts
    assert fact.object_entity_ref == "Celine"


def test_merge_extractions_dedups_a_fact_restated_across_groups() -> None:
    shared = _mr_rel("Me", "Bob", predicate="sibling")
    merged = merge_extractions([_mr_part(facts=[shared]), _mr_part(facts=[shared])])
    assert len(merged.facts) == 1
