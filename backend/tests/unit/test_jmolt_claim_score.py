"""Scoring the claim gate before it may refuse anything (JMOLT_LEDGER_ENGINE_PLAN.md, S2).

The rule this enforces is the direct lesson of the fix that did not work: showing jmolt its own
post titles was reasoned about, shipped, and demonstrably failed, and nothing between the
reasoning and the failure would have caught it because nothing measured it.

The asymmetry is the whole design here, so it is what these tests pin: a miss leaves the status
quo, while a false refusal spends the night's one good thought and — with a retry allowed —
teaches the model to rephrase until it gets through, which is repetition the gate can no longer
see.
"""

import pytest

from jbrain.agent.jmolt_claim import Claim
from jbrain.agent.jmolt_claim_score import LabelledPair, best_threshold, score_gate

pytestmark = pytest.mark.anyio


class _Embed:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._v = vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._v[t] for t in texts]


def _pair(prior: Claim, candidate: Claim, *, restatement: bool, note: str = "") -> LabelledPair:
    return LabelledPair(prior=prior, candidate=candidate, is_restatement=restatement, note=note)


# Three real-shaped cases: a restatement in different words, a changed view, and something
# unrelated. Vectors are stated so a test asserts the SCORER, not an embedding model.
A = Claim.of("self audit", "REDUCES_TO", "generation")
A_AGAIN = Claim.of("verification", "REDUCES_TO", "another inference pass")
A_CHANGED = Claim.of("self audit", "REDUCES_TO", "a second pass with different priors")
UNRELATED = Claim.of("weeks", "IS", "an odd unit for an agent")

_VECTORS = {
    A.text: [1.0, 0.0, 0.0],
    A_AGAIN.text: [0.97, 0.24, 0.0],  # ~0.97 with A
    A_CHANGED.text: [0.97, 0.24, 0.0],
    UNRELATED.text: [0.0, 1.0, 0.0],
}
EMBED = _Embed(_VECTORS)


async def test_a_restatement_is_counted_as_caught_when_refused() -> None:
    score = await score_gate([_pair(A, A_AGAIN, restatement=True)], EMBED, threshold=0.88)
    assert (score.caught, score.missed, score.false_refusals) == (1, 0, 0)
    assert score.catch_rate == 1.0


async def test_a_restatement_the_gate_lets_through_is_reported_with_its_reason() -> None:
    """A miss is tolerable, but it must be legible: "which ones got through, and on what
    grounds" is the only thing that tells you whether to move the threshold or fix an
    exception."""
    score = await score_gate(
        [_pair(A, A_AGAIN, restatement=True, note="the developing-view pair")],
        EMBED,
        threshold=0.999,
    )
    assert score.missed == 1
    assert "the developing-view pair" in score.misses[0]
    assert "not close to anything said before" in score.misses[0]


async def test_a_changed_view_wrongly_refused_is_the_expensive_error() -> None:
    """It is not scored as a mere inaccuracy — it is what makes a threshold unusable."""
    # A changed view whose exception is defeated: same similarity, but the stem differs so
    # supersession does not apply and there is no new citation either.
    disguised = Claim.of("verification", "REDUCES_TO", "another inference pass")
    score = await score_gate([_pair(A, disguised, restatement=False)], EMBED, threshold=0.88)
    assert score.false_refusals == 1
    assert score.usable is False
    assert "refused as" in score.wrongly_refused[0]


async def test_a_threshold_that_catches_nothing_is_not_usable_either() -> None:
    """A gate that never refuses is not a safe gate, it is an absent one — and shipping it
    would let us believe the problem was addressed."""
    score = await score_gate([_pair(A, A_AGAIN, restatement=True)], EMBED, threshold=0.999)
    assert score.false_refusals == 0 and score.caught == 0
    assert score.usable is False


async def test_the_chosen_threshold_is_the_loosest_that_works_not_the_most_accurate() -> None:
    """Loosest, because of the asymmetry: we would rather tolerate a repeat than eat a real
    thought and teach the model to write around the gate."""
    pairs = [
        _pair(A, A_AGAIN, restatement=True),
        _pair(A, A_CHANGED, restatement=False),  # rescued by the supersession exception
        _pair(A, UNRELATED, restatement=False),
    ]
    chosen, scores = await best_threshold(pairs, EMBED)
    assert chosen is not None
    usable = [s.threshold for s in scores if s.usable]
    assert chosen == max(usable)
    # And at the chosen threshold nothing new was refused, which is the absolute condition.
    [at_chosen] = [s for s in scores if s.threshold == chosen]
    assert at_chosen.false_refusals == 0 and at_chosen.caught == 1


async def test_no_usable_threshold_is_a_result_and_means_the_gate_does_not_ship() -> None:
    """The point of scoring first: "we could not find a setting that works" has to be a
    possible answer, or the score is decoration on a decision already made."""
    impossible = [
        _pair(A, A_AGAIN, restatement=True),
        # Identical vectors AND identical stems and citations, but labelled new: no threshold
        # can separate them, so every threshold that catches the restatement refuses this.
        _pair(
            A, Claim.of("verification", "REDUCES_TO", "another inference pass"), restatement=False
        ),
    ]
    chosen, _ = await best_threshold(impossible, EMBED)
    assert chosen is None


async def test_each_pair_is_judged_against_only_its_own_prior() -> None:
    """Carrying state between pairs would let one pair's outcome move another's, and the score
    would then describe an ordering rather than a threshold."""
    pairs = [
        _pair(A, A_AGAIN, restatement=True),
        _pair(UNRELATED, A_AGAIN, restatement=False),  # far from ITS prior, so allowed
    ]
    score = await score_gate(pairs, EMBED, threshold=0.88)
    assert (score.caught, score.false_refusals, score.allowed_new) == (1, 0, 1)


async def test_rates_are_none_rather_than_zero_when_nothing_was_measured() -> None:
    """An unmeasured rate must not read like a perfect one."""
    score = await score_gate([], EMBED, threshold=0.88)
    assert score.catch_rate is None and score.false_refusal_rate is None
    assert score.usable is False
