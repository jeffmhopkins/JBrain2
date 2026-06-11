"""The eval scorer is pure and CI-safe (no live model): pin its matching so a
green prompt-eval run actually means what it says. The live run itself
(evals.run against a real provider) is opt-in and never part of CI."""

from datetime import UTC, datetime
from typing import Any

from evals.run import _overlaps, _score

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedMention,
    ExtractedToken,
    Extraction,
)


def _mention(name: str) -> ExtractedMention:
    return ExtractedMention(name=name, kind="Person", surface_text=name)


def _edge(predicate: str, obj: str) -> ExtractedFact:
    return ExtractedFact(
        predicate=predicate, qualifier="", kind="relationship", statement="", value_json=None,
        assertion="asserted", entity_ref="X", object_entity_ref=obj, temporal=None,
        domain="general", confidence=0.9,
    )  # fmt: skip


def _extraction(
    mentions: list[ExtractedMention],
    facts: list[ExtractedFact] | None = None,
    tokens: list[ExtractedToken] | None = None,
) -> Extraction:
    return Extraction(title="", tags=[], mentions=mentions, facts=facts or [], tokens=tokens or [])


def test_overlaps_tolerates_first_name_vs_full_name() -> None:
    assert _overlaps("Celine", "Celine Hopkins")
    assert _overlaps("Celine Hopkins", "Celine")
    assert not _overlaps("Celine", "Jeff")
    assert not _overlaps("", "Jeff")


def test_score_person_recall_passes_and_fails() -> None:
    case: dict[str, Any] = {"name": "m", "expect": {"person_mentions": ["Jeff", "Celine Hopkins"]}}
    ok = _score(case, _extraction([_mention("Jeff"), _mention("Celine Hopkins")]))
    assert ok.passed
    # Dropping the object person (the actual lapse) fails the case.
    missed = _score(case, _extraction([_mention("Jeff")]))
    assert not missed.passed
    assert any(label == "person:Celine Hopkins" and not ok_ for label, ok_, _ in missed.checks)


def test_score_edge_matches_on_object_entity_ref() -> None:
    case: dict[str, Any] = {
        "name": "e",
        "expect": {"edges": [{"predicate": "spouse", "object": "Celine"}]},
    }
    ok = _score(case, _extraction([_mention("Jeff")], [_edge("spouse", "Celine Hopkins")]))
    assert ok.passed
    # A bare fact with no object_entity_ref (object left in the statement) fails.
    bare = _score(case, _extraction([_mention("Jeff")], [_edge("spouse", "")]))
    assert not bare.passed


def test_score_temporal_checks_resolved_date_on_fact_or_token() -> None:
    case: dict[str, Any] = {
        "name": "t",
        "expect": {"temporal": [{"phrase": "last night", "resolved_date": "2026-06-10"}]},
    }
    tok = ExtractedToken(
        phrase="last night", kind="point",
        resolved_start=datetime(2026, 6, 10, 22, tzinfo=UTC), resolved_end=None,
        precision="day", rrule=None,
    )  # fmt: skip
    assert _score(case, _extraction([], [], [tok])).passed
    wrong = ExtractedToken(
        phrase="last night", kind="point",
        resolved_start=datetime(2026, 6, 11, 22, tzinfo=UTC), resolved_end=None,
        precision="day", rrule=None,
    )  # fmt: skip
    assert not _score(case, _extraction([], [], [wrong])).passed
