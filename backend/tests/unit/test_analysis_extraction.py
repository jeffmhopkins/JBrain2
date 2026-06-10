"""Extraction parsing, the domain ratchet, and prompt assembly — all pure."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from jbrain.analysis.extraction import (
    ExtractionError,
    parse_datetime,
    parse_extraction,
    ratchet_domain,
)
from jbrain.analysis.prompt import (
    CANONICAL_PREDICATES,
    EXTRACTION_SCHEMA,
    MAX_FACTS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
    format_anchor,
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


def test_naive_datetimes_pin_to_the_capture_frame_not_utc() -> None:
    """A model echoing the anchor's local date without an offset means LOCAL
    June 10 — pinning it to UTC shifted every day-precision date a day early
    when rendered locally (the field off-by-one)."""
    tz = timezone(timedelta(hours=-6))
    pinned = parse_datetime("2026-06-10T00:00:00", default_tz=tz)
    assert pinned is not None and pinned == datetime(2026, 6, 10, tzinfo=tz)
    assert pinned.astimezone(tz).date().isoformat() == "2026-06-10"
    # An explicit offset from the model always wins over the default.
    explicit = parse_datetime("2026-06-10T00:00:00+02:00", default_tz=tz)
    assert explicit is not None and explicit.utcoffset() == timedelta(hours=2)


def test_parse_extraction_threads_the_capture_frame() -> None:
    tz = timezone(timedelta(hours=-6))
    payload = valid_payload()
    payload["facts"][0]["temporal"]["resolved_start"] = "2026-06-10T00:00:00"
    payload["temporal_tokens"][0]["resolved_start"] = "2026-06-10T00:00:00"
    parsed = parse_extraction(payload, default_tz=tz)
    fact = parsed.facts[0]
    assert fact.temporal is not None
    assert fact.temporal.resolved_start == datetime(2026, 6, 10, tzinfo=tz)
    assert parsed.tokens[0].resolved_start == datetime(2026, 6, 10, tzinfo=tz)


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


def test_anchor_is_stated_in_the_authors_local_frame() -> None:
    """Field regression: an evening-local capture must anchor as the LOCAL
    date with weekday and offset — a UTC-frame anchor let the model resolve
    "today" a day off the author's calendar."""
    tz = timezone(timedelta(hours=-6))
    anchor = datetime(2026, 6, 10, 17, 11, 42, tzinfo=tz)
    prompt = build_user_prompt(["Saw Dr. Patel today."], anchor=anchor, domain="general")
    assert "Wednesday, June 10, 2026, 5:11 PM (UTC-06:00)" in prompt
    assert "2026-06-10T17:11:42-06:00" in prompt
    # The resolution frame is spelled out, not left to UTC day arithmetic.
    assert '"today" = 2026-06-10' in prompt


def test_format_anchor_covers_offsets_and_meridiem() -> None:
    tz = timezone(timedelta(hours=5, minutes=30))
    assert (
        format_anchor(datetime(2026, 1, 5, 0, 7, tzinfo=tz))
        == "Monday, January 5, 2026, 12:07 AM (UTC+05:30)"
    )
    assert (
        format_anchor(datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
        == "Wednesday, June 10, 2026, 12:00 PM (UTC+00:00)"
    )


def test_v2_prompt_pins_kind_discipline_and_canonical_predicates() -> None:
    """Drift guard for the v2 quality fixes: the worked relocation example
    (the exact field failure), the canonical predicate list, and the
    future-tense rules must survive prompt edits — or bump the version."""
    assert PROMPT_VERSION == "note-extract-v2"
    for predicate in CANONICAL_PREDICATES:
        assert predicate in SYSTEM_PROMPT, predicate
    for needle in (
        # The field failure, worked: a move is a state change, not a bare event.
        'moved to Denver" -> MANDATORY state fact',
        '{"city": "Denver"}',
        "KIND DISCIPLINE",
        'relocatedTo Denver" — optionally add the move event, but never instead',
        'started at Acme on Monday" -> MANDATORY state fact',
        # Predicate convergence: one spelling per concept, snake_case is last resort.
        "The same concept must ALWAYS get the same predicate",
        "never residence, moved_to, relocatedTo, address",
        "coin a snake_case predicate only for a genuinely novel concept",
        # Future tense: expected assertion + absolute time in the statement.
        'are ALWAYS "expected"',
        "Follow-up with Dr. Patel around September",
        # Local-frame temporal resolution.
        "IN THE AUTHOR'S LOCAL FRAME",
        '"today" = 2026-06-10T00:00:00-06:00',
    ):
        assert needle in SYSTEM_PROMPT, needle
    # The v1 guardrails the brief keeps: soft cap, honest confidence,
    # per-domain title safety, assertion levels.
    for kept in (
        str(MAX_FACTS),
        "honest",
        "never surface health or finance details in the title",
        "hypothetical",
    ):
        assert kept in SYSTEM_PROMPT, kept


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
