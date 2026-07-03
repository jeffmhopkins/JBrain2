"""The wiki_lint calibration scorer (jbrain.evals.wiki_lint_runner): the deterministic scoring
core — verdict extraction, precision/recall confusion, and PASS/FAIL reporting — with a FAKE
completer so CI never calls a model. The gateway/CLI shell is `pragma: no cover` (run manually)."""

from typing import Any

from jbrain.evals.wiki_lint_runner import (
    Confusion,
    load_wiki_lint_cases,
    report,
    score_wiki_lint_cases,
)


def _completer(verdict: dict[str, Any] | None):
    """A fake Completer that always returns the given single-item verdict list (or a bad shape)."""

    async def complete(
        system: str, user_text: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        return None if verdict is None else {"verdicts": [verdict]}

    return complete


async def test_scores_a_true_positive_and_true_negative() -> None:
    cases = [
        {
            "name": "c_pos",
            "kind": "contradiction",
            "a_claims": ["x"],
            "b_claims": ["y"],
            "should_fire": True,
        },
        {
            "name": "c_neg",
            "kind": "contradiction",
            "a_claims": ["x"],
            "b_claims": ["y"],
            "should_fire": False,
        },
    ]
    fires = await score_wiki_lint_cases(cases, _completer({"index": 0, "contradiction": True}))
    # The model says "contradiction" for both: the positive is correct, the negative is a false pos.
    assert [r.predicted for r in fires] == [True, True]
    assert [r.ok for r in fires] == [True, False]


async def test_stale_dimension_and_unparseable_is_an_error() -> None:
    cases = [
        {
            "name": "s_pos",
            "kind": "stale",
            "superseded_fact": "f",
            "prose": "p",
            "should_fire": True,
        },
        {
            "name": "s_err",
            "kind": "stale",
            "superseded_fact": "f",
            "prose": "p",
            "should_fire": True,
        },
    ]
    quiet = await score_wiki_lint_cases(
        cases[:1], _completer({"index": 0, "framed_as_current": True})
    )
    assert quiet[0].ok and quiet[0].predicted is True
    # A None/garbage reply → predicted is None → an error, never silently a False.
    errored = await score_wiki_lint_cases(cases[1:], _completer(None))
    assert errored[0].predicted is None and not errored[0].ok


def test_confusion_precision_recall() -> None:
    c = Confusion()
    for r in [
        type("R", (), {"predicted": True, "should_fire": True})(),  # TP
        type("R", (), {"predicted": True, "should_fire": False})(),  # FP
        type("R", (), {"predicted": False, "should_fire": True})(),  # FN
        type("R", (), {"predicted": False, "should_fire": False})(),  # TN
        type("R", (), {"predicted": None, "should_fire": True})(),  # error
    ]:
        c.add(r)
    assert (c.tp, c.fp, c.fn, c.tn, c.errors) == (1, 1, 1, 1, 1)
    assert c.precision == 0.5 and c.recall == 0.5


async def test_report_returns_false_on_any_miss() -> None:
    good = await score_wiki_lint_cases(
        [
            {
                "name": "ok",
                "kind": "contradiction",
                "a_claims": ["x"],
                "b_claims": ["y"],
                "should_fire": True,
            }
        ],
        _completer({"index": 0, "contradiction": True}),
    )
    assert report(good) is True
    bad = await score_wiki_lint_cases(
        [
            {
                "name": "fp",
                "kind": "contradiction",
                "a_claims": ["x"],
                "b_claims": ["y"],
                "should_fire": False,
            }
        ],
        _completer({"index": 0, "contradiction": True}),
    )
    assert report(bad) is False


def test_shipped_cases_load_and_are_well_formed() -> None:
    cases = load_wiki_lint_cases()
    assert len(cases) >= 20
    for c in cases:
        assert c["kind"] in ("contradiction", "stale")
        assert isinstance(c["should_fire"], bool)
        if c["kind"] == "contradiction":
            assert c["a_claims"] and c["b_claims"]
        else:
            assert c["superseded_fact"] and c["prose"]
