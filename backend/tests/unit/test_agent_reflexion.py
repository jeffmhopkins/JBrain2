"""Reflexion (Loop 1): the deterministic verifiers and the strict-improvement,
hard-capped retry controller. Pure — no persistence, no real model."""

from jbrain.agent.reflexion import (
    PASS_SCORE,
    Reflection,
    VerificationResult,
    aggregate,
    reflect,
    significant_tokens,
    strictly_improves,
    verify_citations,
    verify_grounding,
    verify_mutation,
)


class TestVerifyCitations:
    def test_no_citations_is_a_clean_pass(self) -> None:
        assert verify_citations([], ["f1"]).passed

    def test_all_in_scope_passes(self) -> None:
        r = verify_citations(["f1", "f2"], ["f1", "f2", "f3"])
        assert r.score == PASS_SCORE and r.issues == ()

    def test_out_of_scope_citation_is_flagged_and_scored_down(self) -> None:
        r = verify_citations(["f1", "ghost"], ["f1"])
        assert r.score == 0.5
        assert r.issues == ("cited fact not in scope: ghost",)


class TestVerifyGrounding:
    def test_grounded_claim_passes(self) -> None:
        r = verify_grounding(["cholesterol is elevated"], ["the cholesterol reading is elevated"])
        assert r.passed

    def test_ungrounded_claim_is_flagged(self) -> None:
        r = verify_grounding(["the roof needs replacing"], ["cholesterol labs from June"])
        assert r.score == 0.0
        assert "not grounded" in r.issues[0]

    def test_contentless_claim_cannot_be_ungrounded(self) -> None:
        # All stopwords → no significant tokens → nothing to ground.
        assert verify_grounding(["of the"], ["unrelated"]).passed

    def test_no_claims_passes(self) -> None:
        assert verify_grounding([], ["anything"]).passed


class TestVerifyMutation:
    def test_all_required_present_passes(self) -> None:
        assert verify_mutation({"name": "x", "value": 1}, ["name", "value"]).passed

    def test_missing_or_empty_required_fails(self) -> None:
        r = verify_mutation({"name": "x", "value": ""}, ["name", "value"])
        assert r.score == 0.0
        assert r.issues == ("mutation missing required field: value",)


class TestAggregate:
    def test_means_scores_and_concatenates_issues(self) -> None:
        r = aggregate([VerificationResult(1.0, ()), VerificationResult(0.0, ("bad",))])
        assert r.score == 0.5 and r.issues == ("bad",)

    def test_no_verifiers_is_a_pass(self) -> None:
        assert aggregate([]).passed

    def test_significant_tokens_drops_stopwords_and_short(self) -> None:
        assert significant_tokens("the BP is OK") == {"bp", "ok"}


class TestReflectController:
    def test_strictly_improves_requires_a_higher_score(self) -> None:
        assert strictly_improves(VerificationResult(0.6, ()), VerificationResult(0.5, ()))
        assert not strictly_improves(VerificationResult(0.5, ()), VerificationResult(0.5, ()))

    async def test_a_passing_first_answer_never_retries(self) -> None:
        calls = {"n": 0}

        async def produce() -> tuple[str, VerificationResult]:
            calls["n"] += 1
            return "answer", VerificationResult(PASS_SCORE, ())

        out = await reflect(produce)
        assert out == Reflection("answer", VerificationResult(PASS_SCORE, ()), 0)
        assert calls["n"] == 1  # no retry

    async def test_adopts_a_strictly_improving_retry(self) -> None:
        scripted = [
            ("draft", VerificationResult(0.4, ("issue",))),
            ("better", VerificationResult(1.0, ())),
        ]

        async def produce() -> tuple[str, VerificationResult]:
            return scripted.pop(0)

        out = await reflect(produce)
        assert out.answer == "better" and out.result.passed and out.retries == 1

    async def test_keeps_the_incumbent_when_a_retry_does_not_improve(self) -> None:
        scripted = [
            ("draft", VerificationResult(0.6, ("issue",))),
            ("worse", VerificationResult(0.3, ("issue",))),
        ]

        async def produce() -> tuple[str, VerificationResult]:
            return scripted.pop(0)

        out = await reflect(produce)
        # The worse retry is discarded; we keep the better incumbent and stop.
        assert out.answer == "draft" and out.result.score == 0.6 and out.retries == 1

    async def test_hard_caps_retries_at_two(self) -> None:
        # Every attempt strictly improves but never passes — the cap stops it.
        scores = [0.1, 0.2, 0.3, 0.4, 0.5]

        async def produce() -> tuple[str, VerificationResult]:
            return "a", VerificationResult(scores.pop(0), ("issue",))

        out = await reflect(produce, max_retries=2)
        assert out.retries == 2
        assert out.result.score == 0.3  # 1 initial + 2 retries = 3 produce() calls
