"""Extraction parsing, the domain ratchet, and prompt assembly — all pure."""

from datetime import UTC, datetime
from typing import Any

import pytest

from jbrain.analysis.extraction import (
    ExtractionError,
    parse_datetime,
    parse_extraction,
    ratchet_domain,
)
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


def test_user_prompt_carries_anchor_with_timezone_domain_and_content() -> None:
    anchor = datetime(2026, 6, 10, 9, 30, tzinfo=UTC)
    prompt = build_user_prompt(["BP was 118/76", "second chunk"], anchor=anchor, domain="health")
    assert "2026-06-10T09:30:00+00:00" in prompt
    assert "health" in prompt
    assert "BP was 118/76" in prompt and "second chunk" in prompt


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
