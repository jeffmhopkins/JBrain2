"""Reflexion (Loop 1): the deterministic verifiers and the strict-improvement,
hard-capped retry controller. Pure — no persistence, no real model."""

from jbrain.agent.reflexion import (
    _GROUNDING_THRESHOLD,
    PASS_SCORE,
    Reflection,
    VerificationResult,
    aggregate,
    claims_from,
    critique_worthy,
    reflect,
    significant_tokens,
    strictly_improves,
    verify_citations,
    verify_grounding,
    verify_mutation,
)


class TestCritiqueWorthy:
    def test_greeting_with_no_sources_or_mutation_is_not_worthy(self) -> None:
        assert not critique_worthy(source_count=0, mutated=False, scopes=["general"])

    def test_surfaced_sources_make_a_turn_worthy(self) -> None:
        assert critique_worthy(source_count=2, mutated=False, scopes=["general"])

    def test_a_staged_mutation_makes_a_turn_worthy(self) -> None:
        assert critique_worthy(source_count=0, mutated=True, scopes=["general"])

    def test_a_sensitive_scope_makes_a_turn_worthy(self) -> None:
        # Health/finance/location carry real-world consequence — verify even a
        # bare answer that cited nothing and staged nothing.
        assert critique_worthy(source_count=0, mutated=False, scopes=["health"])
        assert critique_worthy(source_count=0, mutated=False, scopes=["general", "finance"])
        assert critique_worthy(source_count=0, mutated=False, scopes=["location"])

    def test_general_only_chitchat_is_never_worthy(self) -> None:
        assert not critique_worthy(source_count=0, mutated=False, scopes=[])
        assert not critique_worthy(source_count=0, mutated=False, scopes=["general"])


class TestClaimsFrom:
    def test_splits_on_sentence_punctuation(self) -> None:
        # A boundary is .!? followed by whitespace; trailing punctuation with no
        # following space stays on the last claim (harmless — grounding ignores it).
        assert claims_from("The BP is fine. Cholesterol is high! Really?") == [
            "The BP is fine",
            "Cholesterol is high",
            "Really?",
        ]

    def test_splits_on_newlines_and_drops_blanks(self) -> None:
        assert claims_from("line one\n\nline two\n") == ["line one", "line two"]

    def test_empty_answer_yields_no_claims(self) -> None:
        assert claims_from("   ") == []


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


class TestGroundingThresholdCalibration:
    """Pins the `_GROUNDING_THRESHOLD=0.5` choice (Track R4). There is no
    answer-grounding gold corpus in the repo (tests/eval/corpus targets the
    extraction/integration chain, not chat-answer grounding), so the threshold is
    kept at the conservative default and its behavior characterized here: 0.5 means
    "at least half a claim's content tokens must appear in the retrieved sources".
    Tuned UP it would flag more partially-grounded answers (more false positives,
    noisier verdicts); tuned DOWN it would pass weakly-grounded ones (more false
    negatives, missed warnings). 0.5 is deliberately lenient — Loop 1 in mode (b)
    only *annotates*, so a missed verdict is cheaper than a noisy one on every
    paraphrase."""

    def test_default_threshold_is_one_half(self) -> None:
        assert _GROUNDING_THRESHOLD == 0.5

    def test_exactly_half_overlap_is_grounded_at_the_default(self) -> None:
        # 2 of 4 content tokens overlap (= 0.5) → grounded (>= threshold).
        r = verify_grounding(["alpha beta gamma delta"], ["alpha beta omega"])
        assert r.passed

    def test_just_under_half_is_flagged(self) -> None:
        # 1 of 3 content tokens overlap (~0.33 < 0.5) → ungrounded.
        r = verify_grounding(["alpha gamma delta"], ["alpha omega"])
        assert r.score == 0.0 and "not grounded" in r.issues[0]

    def test_a_stricter_threshold_would_flag_a_partial_paraphrase(self) -> None:
        # The same half-overlap claim a stricter 0.75 cutoff would (over-)flag —
        # the false-positive cost that keeps the default at the lenient 0.5.
        partial = verify_grounding(["alpha beta gamma delta"], ["alpha beta omega"], threshold=0.75)
        assert not partial.passed


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
