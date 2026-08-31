"""The claim gate (JMOLT_LEDGER_ENGINE_PLAN.md, S2).

The gate's job is to decide what NOT to say without ever asking the model whether it repeated
itself. So the tests are about the DECISION: what it refuses, and — more importantly — the two
things it must let through, because a gate that only refuses teaches an agent to say nothing
rather than to develop a view.
"""

import pytest

from jbrain.agent.jmolt_claim import (
    DEFAULT_PREDICATE,
    Claim,
    ClaimGate,
    normalize_predicate,
)

pytestmark = pytest.mark.anyio


class _Embed:
    """Vectors stated by the test, so a test asserts the gate's LOGIC rather than an
    embedding model's opinion of two sentences."""

    def __init__(self, vectors: dict[str, list[float]], default: list[float] | None = None) -> None:
        self._v = vectors
        self._default = default or [0.0, 0.0, 1.0]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._v.get(t, self._default) for t in texts]


SAME = [1.0, 0.0, 0.0]
NEAR = [0.99, 0.14, 0.0]  # cosine ~0.99 with SAME
FAR = [0.0, 1.0, 0.0]


# --- the closed predicate set ----------------------------------------------


def test_a_predicate_folds_onto_the_closed_set() -> None:
    assert normalize_predicate("reduces_to") == "REDUCES_TO"
    assert normalize_predicate("is just") == "REDUCES_TO"
    assert normalize_predicate("depends on") == "REQUIRES"


def test_an_unknown_predicate_becomes_the_weak_fallback_never_a_new_one() -> None:
    """A set that grows on demand is not closed, and two spellings of one relation would stop
    being neighbours the moment the model varied its wording."""
    assert normalize_predicate("ENTAILS_SOMEWHAT") == DEFAULT_PREDICATE
    assert normalize_predicate("") == DEFAULT_PREDICATE


def test_a_term_is_normalized_so_two_spellings_are_one_subject() -> None:
    """This is load-bearing for the supersession exception, which compares subject and
    predicate by EXACT match: "the self-audit" and "self audit" have to be one subject, or a
    changed view about one of them reads as an unrelated new claim and sails through."""
    assert Claim.of("The Self-Audit!", "is", "Generation").subject == "self audit"
    assert Claim.of("self audit", "is", "x").stem == Claim.of("The self-audit", "IS", "y").stem


def test_a_term_that_is_only_an_article_keeps_it() -> None:
    """Stripping to nothing would make every such claim share a subject with every other."""
    assert Claim.of("the", "IS", "a").subject == "the"


def test_the_embedded_text_is_the_triple_spelled_plainly() -> None:
    """The embedding model has never seen our pipe notation; `a | REDUCES_TO | b` embeds as
    punctuation."""
    assert Claim.of("self audit", "REDUCES_TO", "generation").text == (
        "self audit reduces to generation"
    )


# --- the decision -----------------------------------------------------------


async def test_the_first_claim_of_a_night_is_always_allowed() -> None:
    gate = ClaimGate()
    verdict = await gate.judge(Claim.of("weeks", "IS", "odd"), _Embed({}))
    assert verdict.allowed and verdict.reason == "nothing claimed yet"


async def test_something_new_gets_through() -> None:
    prior = Claim.of("self audit", "REDUCES_TO", "generation")
    candidate = Claim.of("weeks", "IS", "an odd unit")
    embed = _Embed(
        {prior.text: SAME, candidate.text: FAR, prior.object: SAME, candidate.object: FAR}
    )
    gate = ClaimGate()
    await gate.load([prior], embed)
    assert (await gate.judge(candidate, embed)).allowed


async def test_saying_the_same_thing_again_with_nothing_new_is_refused() -> None:
    """The failure this gate exists for: three of four posts on the observed night restated
    the same claim in different words."""
    prior = Claim.of("self audit", "REDUCES_TO", "generation")
    candidate = Claim.of("verification", "REDUCES_TO", "another inference pass")
    embed = _Embed(
        {prior.text: SAME, candidate.text: NEAR, prior.object: SAME, candidate.object: NEAR}
    )
    gate = ClaimGate()
    await gate.load([prior], embed)
    verdict = await gate.judge(candidate, embed)
    assert verdict.refused
    assert verdict.reason == "already said, with nothing new"
    assert verdict.nearest == prior  # and it can say WHAT it repeated


async def test_a_changed_view_is_development_and_gets_through() -> None:
    """Same territory, different conclusion — measured on the OBJECTS, not on string equality
    of subject and predicate. The box's own extractions killed the equality version: six real
    posts making one claim produced five spellings of the subject and four predicates."""
    prior = Claim.of("self audit", "REDUCES_TO", "generation")
    candidate = Claim.of("self audit", "REDUCES_TO", "a second pass with different priors")
    embed = _Embed(
        {
            prior.text: SAME,
            candidate.text: NEAR,  # same territory
            prior.object: SAME,
            candidate.object: FAR,  # different conclusion
        }
    )
    gate = ClaimGate()
    await gate.load([prior], embed)
    verdict = await gate.judge(candidate, embed)
    assert verdict.allowed and "different conclusion" in verdict.reason


async def test_the_same_conclusion_in_different_words_is_still_a_repeat() -> None:
    """The loophole the equality version had: reword the object and a restatement reads as a
    changed view. Measuring the objects closes it — the words differ, the conclusion does
    not."""
    prior = Claim.of("owner prompt", "IS", "initial seed")
    candidate = Claim.of("owner's initial prompt", "REDUCES_TO", "a seed value")
    embed = _Embed(
        {
            prior.text: SAME,
            candidate.text: NEAR,
            prior.object: SAME,
            candidate.object: NEAR,  # said differently, meaning the same
        }
    )
    gate = ClaimGate()
    await gate.load([prior], embed)
    assert (await gate.judge(candidate, embed)).refused


async def test_the_same_view_with_new_evidence_gets_through() -> None:
    """The weaker exception, and still development: an unchanged claim that now cites
    something is a claim someone can check."""
    prior = Claim.of("self audit", "REDUCES_TO", "generation", citations=["p1"])
    candidate = Claim.of("self audit", "REDUCES_TO", "generation", citations=["p1", "p9"])
    embed = _Embed(
        {prior.text: SAME, candidate.text: SAME, prior.object: SAME, candidate.object: SAME}
    )
    gate = ClaimGate()
    await gate.load([prior], embed)
    verdict = await gate.judge(candidate, embed)
    assert verdict.allowed and "p9" in verdict.reason


async def test_re_citing_the_same_source_is_not_new_evidence() -> None:
    """Otherwise the exception is a loophole any repeat can walk through by repeating its
    citation too."""
    prior = Claim.of("self audit", "REDUCES_TO", "generation", citations=["p1"])
    candidate = Claim.of("self audit", "REDUCES_TO", "generation", citations=["p1"])
    embed = _Embed(
        {prior.text: SAME, candidate.text: SAME, prior.object: SAME, candidate.object: SAME}
    )
    gate = ClaimGate()
    await gate.load([prior], embed)
    assert (await gate.judge(candidate, embed)).refused


async def test_judging_a_draft_does_not_record_it() -> None:
    """A caller that judges a draft and then discards it must not have poisoned the
    comparison for the draft it keeps."""
    prior = Claim.of("weeks", "IS", "odd")
    draft = Claim.of("weeks", "IS", "strange")
    embed = _Embed({prior.text: SAME, draft.text: NEAR, prior.object: SAME, draft.object: NEAR})
    gate = ClaimGate()
    await gate.load([prior], embed)
    await gate.judge(draft, embed)
    second = await gate.judge(draft, embed)
    assert second.nearest == prior  # still only one prior claim to be measured against


async def test_a_remembered_claim_becomes_something_to_repeat() -> None:
    claim = Claim.of("weeks", "IS", "odd")
    embed = _Embed({claim.text: SAME, claim.object: SAME})
    gate = ClaimGate()
    assert (await gate.judge(claim, embed)).allowed  # nothing claimed yet
    await gate.remember(claim, embed)
    assert (await gate.judge(claim, embed)).refused


async def test_the_threshold_is_what_separates_close_from_the_same() -> None:
    """Stated as a knob rather than a constant because the plan forbids shipping this gate
    without an offline score, and this number is the first thing that score exists to set."""
    prior = Claim.of("weeks", "IS", "odd")
    candidate = Claim.of("weeks", "RESEMBLES", "a strange unit")
    embed = _Embed(
        {prior.text: SAME, candidate.text: NEAR, prior.object: SAME, candidate.object: NEAR}
    )

    strict = ClaimGate(threshold=0.999)
    await strict.load([prior], embed)
    assert (await strict.judge(candidate, embed)).allowed  # ~0.99 is not close enough

    loose = ClaimGate(threshold=0.5)
    await loose.load([prior], embed)
    assert (await loose.judge(candidate, embed)).refused


async def test_it_measures_against_the_nearest_prior_claim_not_the_latest() -> None:
    """A night says several things. A repeat of the FIRST one is still a repeat when four
    unrelated claims have been made since."""
    repeated = Claim.of("self audit", "REDUCES_TO", "generation")
    others = [Claim.of(f"thing {i}", "IS", f"other {i}") for i in range(4)]
    candidate = Claim.of("verification", "REDUCES_TO", "another pass")
    embed = _Embed({repeated.text: SAME, candidate.text: NEAR, **{o.text: FAR for o in others}})
    gate = ClaimGate()
    await gate.load([repeated, *others], embed)
    verdict = await gate.judge(candidate, embed)
    assert verdict.refused and verdict.nearest == repeated
