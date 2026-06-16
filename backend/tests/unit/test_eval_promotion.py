"""The promotion gate: a candidate is promoted only with a win on the new case and
no task OR safety regression on the existing set (docs/archive/ASSISTANT_PLAN.md Phase 5)."""

from jbrain.workflow.promotion import (
    EvalRun,
    FixtureScore,
    PromotionResult,
    mean_scores,
    promotion_decision,
)


def run(version: str, *scores: tuple[str, float, float]) -> EvalRun:
    return EvalRun(version, tuple(FixtureScore(f, t, s) for f, t, s in scores))


BASELINE = run("v1", ("a", 1.0, 1.0), ("b", 1.0, 1.0))


def test_promotes_on_a_clean_win() -> None:
    # No regression on a/b, and the new case c passes.
    candidate = run("v2", ("a", 1.0, 1.0), ("b", 1.0, 1.0), ("c", 1.0, 1.0))
    result = promotion_decision(BASELINE, candidate, new_case="c")
    assert result == PromotionResult(True, True, (), (), result.reason)
    assert "promoted" in result.reason


def test_blocks_a_task_regression_even_with_a_new_win() -> None:
    # c wins but b regressed on task — the change traded an old capability.
    candidate = run("v2", ("a", 1.0, 1.0), ("b", 0.5, 1.0), ("c", 1.0, 1.0))
    result = promotion_decision(BASELINE, candidate, new_case="c")
    assert result.promote is False
    assert result.task_regressions == ("b",)
    assert "task regressed" in result.reason


def test_blocks_a_safety_regression_even_when_task_improves() -> None:
    # The key safety-inclusive case: task holds/improves but groundedness drops.
    candidate = run("v2", ("a", 1.0, 0.6), ("b", 1.0, 1.0), ("c", 1.0, 1.0))
    result = promotion_decision(BASELINE, candidate, new_case="c")
    assert result.promote is False
    assert result.safety_regressions == ("a",)
    assert "safety/groundedness regressed" in result.reason


def test_blocks_when_the_new_case_does_not_pass() -> None:
    candidate = run("v2", ("a", 1.0, 1.0), ("b", 1.0, 1.0), ("c", 0.5, 1.0))
    result = promotion_decision(BASELINE, candidate, new_case="c")
    assert result.promote is False
    assert result.new_case_won is False
    assert "did not pass" in result.reason


def test_a_missing_new_case_is_fail_closed() -> None:
    candidate = run("v2", ("a", 1.0, 1.0), ("b", 1.0, 1.0))
    result = promotion_decision(BASELINE, candidate, new_case="c")
    assert result.promote is False and result.new_case_won is False


def test_an_identical_rescore_is_not_a_regression() -> None:
    # Float slack: re-scoring the baseline must not read as a regression.
    candidate = run("v2", ("a", 1.0, 1.0), ("b", 1.0, 1.0), ("c", 1.0, 1.0))
    assert promotion_decision(BASELINE, candidate, new_case="c").promote is True


def test_an_improvement_on_an_existing_fixture_is_allowed() -> None:
    base = run("v1", ("a", 0.5, 1.0), ("b", 1.0, 1.0))
    candidate = run("v2", ("a", 1.0, 1.0), ("b", 1.0, 1.0), ("c", 1.0, 1.0))
    assert promotion_decision(base, candidate, new_case="c").promote is True


def test_mean_scores_headline() -> None:
    assert mean_scores(run("v", ("a", 1.0, 0.5), ("b", 0.0, 0.5))) == (0.5, 0.5)
    assert mean_scores(run("empty")) == (0.0, 0.0)


def test_eval_run_from_cases_splits_task_and_groundedness() -> None:
    from jbrain.evals.runner import CaseResult, eval_run_from_cases

    from jbrain.workflow.promotion import promotion_decision

    # A case where one task check missed and one groundedness guard (absent:) missed.
    case = CaseResult(name="marriage")
    case.checks = [
        ("person:Jeff", True, ""),
        ("edge->Celine", False, ""),  # task miss
        ("absent:Ghost", False, ""),  # a fabricated entity slipped through (groundedness)
    ]
    clean = CaseResult(name="weight")
    clean.checks = [("value:weight~210", True, ""), ("absent:Nobody", True, "")]

    run = eval_run_from_cases([case, clean], "note-extract-v5")
    by = run.by_fixture()
    assert by["marriage"].task == 1 / 3  # only person:Jeff passed of the 3 checks
    assert by["marriage"].safety == 0.0  # the only groundedness guard failed
    assert by["weight"].task == 1.0 and by["weight"].safety == 1.0

    # An errored case is a zero on both dimensions (fail-closed for the gate).
    errored = eval_run_from_cases([CaseResult(name="x", error="boom")], "v")
    assert errored.by_fixture()["x"] == FixtureScore("x", 0.0, 0.0)

    # The adapter feeds straight into the gate.
    baseline = EvalRun("v4", (FixtureScore("weight", 1.0, 1.0),))
    assert promotion_decision(baseline, run, new_case="marriage").promote is False
