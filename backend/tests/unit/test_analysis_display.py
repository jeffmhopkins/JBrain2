"""The review-card / snippet display contract (jbrain.analysis.display):
marked snippets, choice labels, and the invariant that advertised actions are
exactly what the resolve endpoint accepts."""

import json
from pathlib import Path

import pytest

from jbrain.analysis.display import (
    SNIPPET_CHARS,
    ambiguous_display,
    collision_display,
    mark_snippet,
    promotion_display,
    truncation_display,
    value_label,
)

# Shared with the frontend (format.test.ts) — see testdata/value_label_parity.json.
_PARITY = json.loads(
    (Path(__file__).parents[3] / "testdata" / "value_label_parity.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _PARITY, ids=[c["name"] for c in _PARITY])
def test_value_label_matches_the_shared_frontend_contract(case: dict) -> None:
    # The backend value_label and the frontend valueLabel must agree on this
    # fixture (a code review caught backend/frontend drift). Intentional
    # divergences are excluded from the fixture, not asserted here.
    assert value_label(case["value_json"], case["statement"]) == case["expected"]


class TestMarkSnippet:
    def test_wraps_the_cited_span(self) -> None:
        assert mark_snippet("Saw Dr. Patel today.", 4, 13) == "Saw <mark>Dr. Patel</mark> today."

    def test_no_span_serves_the_unmarked_head(self) -> None:
        text = "x" * 500
        assert mark_snippet(text) == "x" * SNIPPET_CHARS
        assert mark_snippet(None) is None

    def test_degenerate_spans_serve_unmarked(self) -> None:
        # Zero-width paraphrase anchors and out-of-range offsets never
        # mismark; they fall back to the plain head.
        assert mark_snippet("hello world", 0, 0) == "hello world"
        assert mark_snippet("hello", 3, 99) == "hello"
        assert mark_snippet("hello", -1, 3) == "hello"

    def test_window_shifts_to_reach_a_deep_span(self) -> None:
        text = "a" * 300 + "NEEDLE" + "b" * 100
        snippet = mark_snippet(text, 300, 306)
        assert snippet is not None
        assert "<mark>NEEDLE</mark>" in snippet
        assert len(snippet) <= SNIPPET_CHARS + len("<mark></mark>")

    def test_span_wider_than_the_window_serves_unmarked(self) -> None:
        text = "z" * 600
        snippet = mark_snippet(text, 10, 590)
        assert snippet is not None and "<mark>" not in snippet


class TestValueLabel:
    def test_blood_pressure_shape(self) -> None:
        label = value_label({"systolic": 128, "diastolic": 82, "unit": "mmHg"}, "BP statement")
        assert label == "128/82 mmHg"
        assert value_label({"systolic": 118.0, "diastolic": 76.0}, "s") == "118/76"

    def test_value_unit_shape(self) -> None:
        assert value_label({"value": 95, "unit": "mg/dL"}, "s") == "95 mg/dL"
        assert value_label({"value": "Denver, CO"}, "s") == "Denver, CO"

    def test_unrecognized_shape_renders_its_string_leaf(self) -> None:
        # An unhandled shape yields its bare datum (the first string leaf) rather
        # than falling through to the statement sentence.
        assert value_label({"street": "99 Pine Ave"}, "Lives at 99 Pine Ave.") == "99 Pine Ave"

    def test_falls_back_to_the_statement_never_empty(self) -> None:
        # No datum in value_json: the statement is the floor — a choice button /
        # value cell must never render empty (it would orphan a review card).
        assert value_label(None, "Sarah works for Ridgeline.") == "Sarah works for Ridgeline."
        assert value_label({}, "He was admitted on Tuesday.") == "He was admitted on Tuesday."

    def test_date_shape_defers_to_the_statement_not_a_raw_iso(self) -> None:
        # The backend has no date formatter; a {start} shape is left to the
        # statement rather than surfacing a raw ISO timestamp as the value.
        assert (
            value_label({"start": "2026-06-15T14:00:00-06:00"}, "Appointment is Monday at 2pm.")
            == "Appointment is Monday at 2pm."
        )

    def test_abbreviated_name_datum_is_not_dropped(self) -> None:
        # A short datum with an internal period (a title, an initial) is the value,
        # never mistaken for a sentence.
        assert value_label({"value": "Dr. Patel"}, "Saw Dr. Patel.") == "Dr. Patel"


class TestReviewDisplays:
    def test_collision_advertises_exactly_the_accept_a_b_actions(self) -> None:
        display = collision_display(
            kind="attribute_collision",
            predicate="birthDate",
            entity_ref="Sarah",
            changed=False,
            label_a="May 2, 1990",
            label_b="March 14, 1988",
            snippet="<mark>March 14</mark>",
        )
        assert display["summary"] == "two values recorded for Sarah's birthDate"
        assert [c["action"] for c in display["choices"]] == ["accept_a", "accept_b"]
        assert [c["label"] for c in display["choices"]] == ["May 2, 1990", "March 14, 1988"]
        # No generic verbs: the footer accept/reject would 400 on this kind.
        assert "outcomes" not in display

    def test_fact_conflict_summary_tracks_the_decision_path(self) -> None:
        changed = collision_display(
            kind="fact_conflict",
            predicate="residence",
            entity_ref="Me",
            changed=True,
            label_a="a",
            label_b="b",
            snippet=None,
        )
        clash = collision_display(
            kind="fact_conflict",
            predicate="bloodPressure",
            entity_ref="Me",
            changed=False,
            label_a="a",
            label_b="b",
            snippet=None,
        )
        assert changed["summary"] == "Me's residence changed"
        assert clash["summary"] == "two bloodPressure values disagree for Me"

    def test_promotion_advertises_accept_and_reject(self) -> None:
        display = promotion_display(
            predicate="address", proposed="general", note_domain="health", snippet=None
        )
        assert set(display["outcomes"]) == {"accept", "reject"}
        assert "general" in display["summary"] and "health" in display["summary"]

    def test_ambiguous_advertises_only_reject(self) -> None:
        # accept would imply a link the backend cannot make yet (layer 2/3).
        display = ambiguous_display(name="Sam", snippet="<mark>Sam</mark> said")
        assert display["summary"] == "which Sam?"
        assert set(display["outcomes"]) == {"reject"}

    def test_truncation_is_informational_and_pluralizes(self) -> None:
        # Like ambiguous_mention it wrote no graph state, so its only verb is a
        # dismissal (reject). Counts read naturally for one vs many.
        many = truncation_display(kept=40, dropped=7, snippet="<mark>…</mark>")
        assert set(many["outcomes"]) == {"reject"}
        assert "kept 40, skipped 7 facts" in many["summary"]
        one = truncation_display(kept=40, dropped=1, snippet=None)
        assert "skipped 1 fact" in one["summary"]
