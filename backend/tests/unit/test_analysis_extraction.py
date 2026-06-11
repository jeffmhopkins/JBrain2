"""Extraction parsing, the domain ratchet, and prompt assembly — all pure."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedTemporal,
    ExtractionError,
    normalize_future_assertion,
    parse_datetime,
    parse_extraction,
    ratchet_domain,
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


def test_prompt_version_bumped_to_v3() -> None:
    assert PROMPT_VERSION == "note-extract-v3"


def test_user_prompt_carries_anchor_with_timezone_domain_and_content() -> None:
    anchor = datetime(2026, 6, 10, 9, 30, tzinfo=UTC)
    prompt = build_user_prompt(["BP was 118/76", "second chunk"], anchor=anchor, domain="health")
    assert "2026-06-10T09:30:00+00:00" in prompt
    assert "health" in prompt
    assert "BP was 118/76" in prompt and "second chunk" in prompt


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
