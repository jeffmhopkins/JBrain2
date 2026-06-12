"""The review-card / snippet display contract (jbrain.analysis.display):
marked snippets, choice labels, and the invariant that advertised actions are
exactly what the resolve endpoint accepts."""

from jbrain.analysis.display import (
    SNIPPET_CHARS,
    ambiguous_display,
    collision_display,
    mark_snippet,
    promotion_display,
    value_label,
)


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

    def test_reference_range_shape(self) -> None:
        # A lab analyte's normal band: prefer the verbatim text, else the bounds.
        band = {
            "low": {"value": 3.5, "unit": "g/dL"},
            "high": {"value": 5.2, "unit": "g/dL"},
            "text": "3.5 - 5.2 g/dL",
        }
        assert value_label(band, "Albumin's normal range is 3.5 - 5.2 g/dL.") == "3.5 - 5.2 g/dL"
        # No text → rendered from the bounds and the shared unit.
        assert (
            value_label(
                {"low": {"value": 3.5, "unit": "g/dL"}, "high": {"value": 5.2, "unit": "g/dL"}}, "s"
            )
            == "3.5 - 5.2 g/dL"
        )
        # One-sided bound still renders, never the statement.
        assert value_label({"high": {"value": 5.2, "unit": "g/dL"}}, "s") == "? - 5.2 g/dL"

    def test_falls_back_to_the_statement(self) -> None:
        # Mirrors the UI's factValue: unrecognized shapes read as the
        # rendered sentence, never raw JSON.
        assert value_label({"street": "99 Pine Ave"}, "Lives at 99 Pine Ave.") == (
            "Lives at 99 Pine Ave."
        )
        assert value_label(None, "Sarah works for Ridgeline.") == "Sarah works for Ridgeline."


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
