"""Score the claim gate offline, before it is ever allowed to refuse a real write.

`JMOLT_LEDGER_ENGINE_PLAN.md` states the rule this file exists to satisfy: **no new guard
ships without an offline score first.** That rule is not procedural caution — it is the direct
lesson of the fix that did not work. Showing jmolt its own post titles so it would stop
restating them was reasoned about, shipped, and demonstrably failed: the 07:09:35 prologue
listed "…my developing view" and that same sitting staged "…a developing view". Nothing
between the reasoning and the failure would have caught it, because nothing measured it.

So a gate gets scored against labelled pairs before it is trusted, and the two numbers that
decide it are not symmetric:

- **A miss** (a real restatement the gate allows) leaves the status quo — an agent that
  repeats itself, which is what we already have.
- **A false refusal** (a genuinely new claim the gate holds) is worse than the disease. It
  spends the night's one good thought, teaches nothing, and — because the gate allows one
  retry — pushes the model to rephrase until it gets through, which is repetition the gate
  can no longer see.

That asymmetry means the threshold is not chosen to maximise accuracy. It is chosen as the
loosest value that still catches the restatements we have, which is what `best_threshold`
reports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from jbrain.agent.jmolt_claim import Claim, ClaimGate, Embedder


@dataclass(frozen=True)
class LabelledPair:
    """One judgement we already know the answer to.

    `prior` is what jmolt had already claimed; `candidate` is what it went on to say.
    `is_restatement` is the human label — taken from a night someone read, never from a
    model's opinion, because a model's opinion is the thing under test.
    """

    prior: Claim
    candidate: Claim
    is_restatement: bool
    note: str = ""


@dataclass
class GateScore:
    threshold: float
    caught: int = 0  # restatements correctly refused
    missed: int = 0  # restatements allowed through
    false_refusals: int = 0  # new claims wrongly refused — the expensive error
    allowed_new: int = 0
    misses: list[str] = field(default_factory=list)
    wrongly_refused: list[str] = field(default_factory=list)

    @property
    def restatements(self) -> int:
        return self.caught + self.missed

    @property
    def catch_rate(self) -> float | None:
        """None, not zero, when there was nothing to catch — an unmeasured rate must not read
        like a perfect one."""
        return self.caught / self.restatements if self.restatements else None

    @property
    def false_refusal_rate(self) -> float | None:
        total = self.false_refusals + self.allowed_new
        return self.false_refusals / total if total else None

    @property
    def usable(self) -> bool:
        """Whether this threshold may ship at all: it must refuse NOTHING that was new.

        Deliberately absolute. A gate that occasionally eats a real thought is one the agent
        learns to write around, and there is no rate of that which is acceptable in exchange
        for catching a repeat we could instead just tolerate."""
        return self.false_refusals == 0 and self.caught > 0


async def score_gate(
    pairs: Sequence[LabelledPair], embed: Embedder, *, threshold: float
) -> GateScore:
    """Run the gate over labelled pairs at one threshold.

    Each pair is judged by a FRESH gate holding only its own prior. Carrying state between
    pairs would let one pair's outcome move another's, and then the score would describe an
    ordering rather than a threshold."""
    score = GateScore(threshold=threshold)
    for pair in pairs:
        gate = ClaimGate(threshold=threshold)
        await gate.load([pair.prior], embed)
        verdict = await gate.judge(pair.candidate, embed)
        label = pair.note or f"{pair.candidate.subject} / {pair.candidate.object}"
        if pair.is_restatement:
            if verdict.refused:
                score.caught += 1
            else:
                score.missed += 1
                score.misses.append(f"{label} — allowed as: {verdict.reason}")
        elif verdict.refused:
            score.false_refusals += 1
            score.wrongly_refused.append(f"{label} — refused as: {verdict.reason}")
        else:
            score.allowed_new += 1
    return score


async def best_threshold(
    pairs: Sequence[LabelledPair],
    embed: Embedder,
    *,
    candidates: Sequence[float] = (0.80, 0.84, 0.86, 0.88, 0.90, 0.92, 0.94, 0.96),
) -> tuple[float | None, list[GateScore]]:
    """The LOOSEST threshold that refuses no new claim and still catches something.

    Loosest, not best. A tighter threshold catches more restatements and eventually starts
    eating real thoughts, and the asymmetry in this module's docstring says which error we
    would rather make. Returns None when no candidate is usable — which is a result, and means
    the gate does not ship."""
    scores = [await score_gate(pairs, embed, threshold=t) for t in candidates]
    usable = [s for s in scores if s.usable]
    # Highest threshold = loosest gate (a claim must be MORE similar to be refused).
    return (max(s.threshold for s in usable) if usable else None), scores
