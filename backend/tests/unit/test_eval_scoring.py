"""The eval scorer is pure and CI-safe (no live model): pin its matching so a
green prompt-eval run actually means what it says. The live run itself
(evals.run against a real provider) is opt-in and never part of CI."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedMention,
    ExtractedToken,
    Extraction,
)
from jbrain.evals.runner import _overlaps, _score, load_cases


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
    assert _overlaps("Vasquez", "Dr. Vasquez")  # punctuation is not a token
    assert not _overlaps("Celine", "Jeff")
    assert not _overlaps("", "Jeff")


def test_overlaps_matches_whole_words_not_substrings() -> None:
    # 'Me' must not match the 'me' inside 'Chase Home Lending' (the live finance
    # eval's lone false-fail) — overlap is whole-word, not raw substring.
    assert not _overlaps("Me", "Chase Home Lending")
    assert not _overlaps("Al", "Alvarez")
    assert _overlaps("Chase", "Chase Home Lending")  # but a whole word still does


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
    valid_expect = {
        "person_mentions", "mentions", "mention_kind", "absent_person",
        "not_person", "edges", "temporal", "value", "domain",
        "absent_edges", "absent_predicates",
    }  # fmt: skip
    domains = {"general", "health", "finance", "location"}
    for c in cases:
        assert c["name"] and c["body"], c
        assert datetime.fromisoformat(c["created_at"]).utcoffset() is not None, c["name"]
        assert set(c.get("expect", {})) <= valid_expect, c["name"]
        for edge in c.get("expect", {}).get("edges", []):
            assert "object" in edge, c["name"]
        for spec in c.get("expect", {}).get("absent_edges", []):
            assert "object" in spec, c["name"]
        for pred in c.get("expect", {}).get("absent_predicates", []):
            assert isinstance(pred, str) and pred, c["name"]
        for tt in c.get("expect", {}).get("temporal", []):
            assert {"phrase", "resolved_date"} <= set(tt), c["name"]
        for mk in c.get("expect", {}).get("mention_kind", []):
            assert mk.get("name") and isinstance(mk.get("kind"), list) and mk["kind"], c["name"]
        for v in c.get("expect", {}).get("value", []):
            assert "contains" in v, c["name"]
        assert set(c.get("expect", {}).get("domain", [])) <= domains, c["name"]


def test_score_not_person_allows_nonperson_mention_but_flags_person() -> None:
    # Over-personification check: a Product/Place/Animal mention is fine, the
    # SAME token typed as a Person is the failure (and absence is fine too).
    case: dict[str, Any] = {"name": "np", "expect": {"not_person": ["Tesla"]}}
    product = ExtractedMention(name="Tesla", kind="Product", surface_text="Tesla")
    assert _score(case, _extraction([product]), _A).passed
    assert _score(case, _extraction([]), _A).passed
    person = ExtractedMention(name="Tesla", kind="Person", surface_text="Tesla")
    assert not _score(case, _extraction([person]), _A).passed


def test_score_mentions_and_mention_kind() -> None:
    org = ExtractedMention(name="Globex Corporation", kind="Organization", surface_text="Globex")
    # Presence by name (any kind).
    assert _score({"name": "m", "expect": {"mentions": ["Globex"]}}, _extraction([org]), _A).passed
    assert not _score({"name": "m", "expect": {"mentions": ["Globex"]}}, _extraction([]), _A).passed
    # Present AND within an allowed kind family (case-insensitive, generous set).
    kind_case: dict[str, Any] = {
        "name": "k",
        "expect": {"mention_kind": [{"name": "Globex", "kind": ["Organization", "Corporation"]}]},
    }
    assert _score(kind_case, _extraction([org]), _A).passed
    wrong = ExtractedMention(name="Globex", kind="Person", surface_text="Globex")
    assert not _score(kind_case, _extraction([wrong]), _A).passed


def test_score_value_matches_measurement_in_fact() -> None:
    fact = ExtractedFact(
        predicate="weight", qualifier="", kind="measurement",
        statement="Weight was 182 lb.", value_json={"value": 182, "unit": "lb"},
        assertion="asserted", entity_ref="Me", object_entity_ref=None, temporal=None,
        domain="health", confidence=0.9,
    )  # fmt: skip
    case: dict[str, Any] = {
        "name": "v",
        "expect": {"value": [{"predicate": "weight", "contains": "182"}]},
    }
    assert _score(case, _extraction([], [fact]), _A).passed
    # Wrong predicate or missing value fails.
    assert not _score(
        {"name": "v", "expect": {"value": [{"predicate": "height", "contains": "182"}]}},
        _extraction([], [fact]),
        _A,
    ).passed
    assert not _score(case, _extraction([]), _A).passed


def test_score_absent_edges_flags_linked_object_but_allows_mention() -> None:
    # Salience negative (v29): the thing may be MENTIONED, but no fact may link
    # it — a one-off event's venue stays in the prose, not on an edge.
    case: dict[str, Any] = {
        "name": "ae",
        "expect": {"absent_edges": [{"object": "lakefront path"}]},
    }
    place = ExtractedMention(name="lakefront path", kind="Place", surface_text="lakefront path")
    assert _score(case, _extraction([place]), _A).passed
    linked = _score(case, _extraction([place], [_edge("ranAt", "lakefront path")]), _A)
    assert not linked.passed
    assert any(label == "absent_edge->lakefront path" and not ok for label, ok, _ in linked.checks)


def test_score_absent_predicates_flags_longtail_fact() -> None:
    case: dict[str, Any] = {"name": "ap", "expect": {"absent_predicates": ["personalBest"]}}
    assert _score(case, _extraction([], []), _A).passed
    assert not _score(case, _extraction([], [_edge("personalBest", "5 miles")]), _A).passed
    # A different predicate doesn't trip the check.
    assert _score(case, _extraction([], [_edge("owns", "Bella")]), _A).passed


def test_score_salience_negatives_are_task_not_groundedness() -> None:
    # The new labels must NOT count into the safety dimension: a salience miss
    # is a task loss, not a fabrication (only "absent:"/"not_person:" are).
    from jbrain.evals.runner import eval_run_from_cases

    case: dict[str, Any] = {
        "name": "sn",
        "expect": {"absent_edges": [{"object": "X"}], "absent_predicates": ["p"]},
    }
    missed = _score(case, _extraction([], [_edge("p", "X")]), _A)
    run = eval_run_from_cases([missed], "v")
    score = run.scores[0]
    assert score.task == 0.0
    assert score.safety == 1.0


def test_score_records_fact_count_for_the_leaner_metric() -> None:
    case: dict[str, Any] = {"name": "fc", "expect": {}}
    res = _score(case, _extraction([], [_edge("owns", "Bella"), _edge("spouse", "Maya")]), _A)
    assert res.fact_count == 2
    assert _score(case, _extraction([]), _A).fact_count == 0


def test_eval_cases_pass_audit() -> None:
    # The offline pre-flight (evals.audit) is enforced here so a new/edited case
    # whose asserted name/number/phrase isn't in its body — or whose closed-set
    # temporal date is off — fails CI before anyone spends a live model call.
    from evals.audit import audit_cases

    assert audit_cases(load_cases()) == []


def test_audit_catches_vacuous_absent_edge() -> None:
    # An absent_edges object the body never names would pass vacuously — the
    # audit flags it as an authoring bug.
    from evals.audit import audit_cases

    case = {
        "name": "bad",
        "body": "Ran along the river this morning.",
        "created_at": "2026-06-11T09:00:00-06:00",
        "expect": {"absent_edges": [{"object": "lakefront path"}]},
    }
    issues = audit_cases([case])
    assert any("absent_edges object" in i for i in issues)


def test_score_domain_checks_per_fact_classification() -> None:
    health_fact = ExtractedFact(
        predicate="bloodPressure", qualifier="", kind="measurement",
        statement="BP 120/80.", value_json={"value": "120/80"}, assertion="asserted",
        entity_ref="Me", object_entity_ref=None, temporal=None, domain="health", confidence=0.9,
    )  # fmt: skip
    case: dict[str, Any] = {"name": "d", "expect": {"domain": ["health"]}}
    assert _score(case, _extraction([], [health_fact]), _A).passed
    # A fact the model left general fails the health-domain check.
    general = ExtractedFact(
        predicate="bloodPressure", qualifier="", kind="measurement", statement="BP 120/80.",
        value_json=None, assertion="asserted", entity_ref="Me", object_entity_ref=None,
        temporal=None, domain="general", confidence=0.9,
    )  # fmt: skip
    assert not _score(case, _extraction([], [general]), _A).passed
