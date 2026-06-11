"""The eval scorer is pure and CI-safe (no live model): pin its matching so a
green prompt-eval run actually means what it says. The live run itself
(evals.run against a real provider) is opt-in and never part of CI."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from evals.run import _overlaps, _score, load_cases

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


# A UTC anchor, so a UTC instant's local date equals its UTC calendar date.
_A = datetime(2026, 6, 11, 12, tzinfo=UTC)


def test_score_person_recall_passes_and_fails() -> None:
    case: dict[str, Any] = {"name": "m", "expect": {"person_mentions": ["Jeff", "Celine Hopkins"]}}
    ok = _score(case, _extraction([_mention("Jeff"), _mention("Celine Hopkins")]), _A)
    assert ok.passed
    # Dropping the object person (the actual lapse) fails the case.
    missed = _score(case, _extraction([_mention("Jeff")]), _A)
    assert not missed.passed
    assert any(label == "person:Celine Hopkins" and not ok_ for label, ok_, _ in missed.checks)


def test_score_edge_matches_on_object_entity_ref() -> None:
    case: dict[str, Any] = {
        "name": "e",
        "expect": {"edges": [{"predicate": "spouse", "object": "Celine"}]},
    }
    ok = _score(case, _extraction([_mention("Jeff")], [_edge("spouse", "Celine Hopkins")]), _A)
    assert ok.passed
    # A bare fact with no object_entity_ref (object left in the statement) fails.
    bare = _score(case, _extraction([_mention("Jeff")], [_edge("spouse", "")]), _A)
    assert not bare.passed


def test_score_temporal_uses_local_date_for_fact_or_token() -> None:
    case: dict[str, Any] = {
        "name": "t",
        "expect": {"temporal": [{"phrase": "last night", "resolved_date": "2026-06-10"}]},
    }
    tok = ExtractedToken(
        phrase="last night", kind="point",
        resolved_start=datetime(2026, 6, 10, 22, tzinfo=UTC), resolved_end=None,
        precision="day", rrule=None,
    )  # fmt: skip
    assert _score(case, _extraction([], [], [tok]), _A).passed
    wrong = ExtractedToken(
        phrase="last night", kind="point",
        resolved_start=datetime(2026, 6, 11, 22, tzinfo=UTC), resolved_end=None,
        precision="day", rrule=None,
    )  # fmt: skip
    assert not _score(case, _extraction([], [], [wrong]), _A).passed
    # The midnight-UTC case the live eval mis-scored: Jun 5 00:00Z is Jun 4 at
    # -06:00, so against a -06:00 anchor it reads as the correct local day.
    mst = ExtractedToken(
        phrase="a week ago", kind="point",
        resolved_start=datetime(2026, 6, 5, 0, tzinfo=UTC), resolved_end=None,
        precision="day", rrule=None,
    )  # fmt: skip
    case_wk: dict[str, Any] = {
        "name": "w",
        "expect": {"temporal": [{"phrase": "a week ago", "resolved_date": "2026-06-04"}]},
    }
    anchor_mst = datetime(2026, 6, 11, 8, 30, tzinfo=timezone(timedelta(minutes=-360)))
    assert _score(case_wk, _extraction([], [], [mst]), anchor_mst).passed


def test_eval_cases_are_wellformed() -> None:
    # Guards against a malformed agent-authored case slipping into the set: every
    # case loads, names are unique, created_at parses with an offset, and only
    # known expect keys are used (a typo'd key would silently never be checked).
    cases = load_cases()
    assert cases
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "duplicate eval case names"
    valid_expect = {"person_mentions", "absent_person", "not_person", "edges", "temporal"}
    for c in cases:
        assert c["name"] and c["body"], c
        assert datetime.fromisoformat(c["created_at"]).utcoffset() is not None, c["name"]
        assert set(c.get("expect", {})) <= valid_expect, c["name"]
        for edge in c.get("expect", {}).get("edges", []):
            assert "object" in edge, c["name"]
        for tt in c.get("expect", {}).get("temporal", []):
            assert {"phrase", "resolved_date"} <= set(tt), c["name"]


def test_score_not_person_allows_nonperson_mention_but_flags_person() -> None:
    # Over-personification check: a Product/Place/Animal mention is fine, the
    # SAME token typed as a Person is the failure (and absence is fine too).
    case: dict[str, Any] = {"name": "np", "expect": {"not_person": ["Tesla"]}}
    product = ExtractedMention(name="Tesla", kind="Product", surface_text="Tesla")
    assert _score(case, _extraction([product]), _A).passed
    assert _score(case, _extraction([]), _A).passed
    person = ExtractedMention(name="Tesla", kind="Person", surface_text="Tesla")
    assert not _score(case, _extraction([person]), _A).passed
