"""The claim gate: decide what NOT to say, without ever asking the model whether it repeated.

S2's novelty check (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md). A candidate reduces to
`subject | predicate | object` over a closed predicate set, and the TRIPLE is embedded — not
the prose.

Why the triple and not the prose, measured rather than assumed. Prose embeddings fail in the
direction that matters: same-author style dominates, so everything an agent writes is
"similar" to everything it writes, and a threshold that catches a real restatement also
catches every unrelated post. Lexical similarity fails the other way — measured Jaccard on
this agent's real duplicate pairs is 0.07-0.20, which is indistinguishable from unrelated
text. `self-audit | REDUCES_TO | generation` and `verification | REDUCES_TO | another
inference pass` are neighbours in triple space and eight lexical points apart.

**Nothing here asks the model to judge itself.** The model proposes a triple — a structured
reduction, which is parsing — and code decides. That division is the third point all six cold
designers made independently: a mid-tier model asked "is this the same as before?" says no,
because the words differ.

**The exception clause is deterministic.** A repeat is held UNLESS it supersedes the prior
claim or cites evidence the prior one lacked — the two behaviours that constitute development
rather than looping. Both are decided from the triple and the citations, never from the
model's account of its own intent:

- **Supersedes**: same subject and predicate, different object. That IS a changed view, and
  it needs no one's opinion to detect.
- **New evidence**: the candidate cites a source the prior claim did not.

One retry maximum, and it is a hard cap rather than a suggestion. A gate that keeps saying
"no, try again" is an adversarial optimiser with the model as its search: it does not teach
restraint, it teaches repetition the gate cannot see.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

# The closed predicate set. Small on purpose: the point of a closed set is that two ways of
# saying the same relation collapse to one, and a set large enough to be expressive is large
# enough to have synonyms in it. Anything unmapped becomes CLAIMS_ABOUT, which is weak enough
# that the object and subject carry the comparison.
PREDICATES = (
    "IS",  # X is Y — definition, identity, category
    "REDUCES_TO",  # X is really just Y
    "CAUSES",  # X produces or leads to Y
    "REQUIRES",  # X cannot happen without Y
    "CONTRADICTS",  # X is inconsistent with Y
    "RESEMBLES",  # X is like Y
    "PREFERS",  # X should be chosen over Y
    "CLAIMS_ABOUT",  # the fallback: X has something to do with Y
)
DEFAULT_PREDICATE = "CLAIMS_ABOUT"

# Synonyms seen in practice, folded to the canonical form. Kept deliberately short — a long
# synonym table is a sign the closed set is doing the wrong job.
_SYNONYMS = {
    "IS_A": "IS",
    "IS_JUST": "REDUCES_TO",
    "REDUCIBLE_TO": "REDUCES_TO",
    "COLLAPSES_TO": "REDUCES_TO",
    "LEADS_TO": "CAUSES",
    "PRODUCES": "CAUSES",
    "NEEDS": "REQUIRES",
    "DEPENDS_ON": "REQUIRES",
    "CONFLICTS_WITH": "CONTRADICTS",
    "DISAGREES_WITH": "CONTRADICTS",
    "LIKE": "RESEMBLES",
    "SIMILAR_TO": "RESEMBLES",
    "BETTER_THAN": "PREFERS",
    "ABOUT": "CLAIMS_ABOUT",
}

# Above this cosine between triples, two claims are the same claim unless an exception applies.
# Provisional: the plan forbids shipping this gate without an offline score, and this number is
# the first thing that score exists to set.
DEFAULT_THRESHOLD = 0.88

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def normalize_predicate(raw: str) -> str:
    """Fold a proposed predicate onto the closed set. Unknown → the weak fallback, never a
    new predicate: a set that grows on demand is not closed, and two spellings of one relation
    would stop being neighbours the moment the model varied its wording."""
    key = re.sub(r"[^A-Z0-9]+", "_", (raw or "").strip().upper()).strip("_")
    if key in PREDICATES:
        return key
    return _SYNONYMS.get(key, DEFAULT_PREDICATE)


# Stripped from the FRONT of a term only. A leading article is noise on a noun phrase, and it
# matters more than it looks: the supersession exception compares subject and predicate by
# exact match, so "the self-audit" and "self-audit" would otherwise be two different subjects
# and a changed view about one of them would read as an unrelated new claim.
_LEADING_ARTICLES = ("the", "a", "an")


def _normalize_term(raw: str) -> str:
    """A term reduced to comparable form: lowercased, punctuation dropped, a leading article
    removed. Deliberately NOT stemming — stemming collapses distinctions the object has to
    keep, and an object is where a changed view shows up."""
    words = _WORD.findall((raw or "").lower())
    if len(words) > 1 and words[0] in _LEADING_ARTICLES:
        words = words[1:]
    return " ".join(words)


@dataclass(frozen=True)
class Claim:
    """One claim, as the gate compares it."""

    subject: str
    predicate: str
    object: str
    # Where the claim's support comes from — Moltbook ids, handles, URLs. The "cites evidence
    # the prior one lacked" exception is decided on this set, so it is part of the claim, not
    # metadata about it.
    citations: frozenset[str] = frozenset()

    @staticmethod
    def of(subject: str, predicate: str, object: str, citations: Sequence[str] = ()) -> Claim:
        return Claim(
            subject=_normalize_term(subject),
            predicate=normalize_predicate(predicate),
            object=_normalize_term(object),
            citations=frozenset(c.strip() for c in citations if c and c.strip()),
        )

    @property
    def text(self) -> str:
        """What gets embedded. The triple, spelled plainly — the embedding model has never
        seen our pipe notation, and `a | REDUCES_TO | b` embeds as punctuation."""
        return f"{self.subject} {self.predicate.lower().replace('_', ' ')} {self.object}"

    @property
    def stem(self) -> tuple[str, str]:
        """Subject and predicate. Two claims sharing a stem with different objects are a
        changed view, which is the supersession exception."""
        return (self.subject, self.predicate)


@dataclass(frozen=True)
class Verdict:
    """What the gate decided, and why — in terms a human reading a night's log can check."""

    allowed: bool
    reason: str
    similarity: float = 0.0
    # The prior claim this was measured against, when there was one.
    nearest: Claim | None = None

    @property
    def refused(self) -> bool:
        return not self.allowed


@dataclass
class ClaimGate:
    """Holds the night's prior claims and decides on candidates."""

    threshold: float = DEFAULT_THRESHOLD
    _claims: list[Claim] = field(default_factory=list)
    _vectors: list[list[float]] = field(default_factory=list)

    async def load(self, claims: Sequence[Claim], embed: Embedder) -> None:
        """Seed the gate with what jmolt has already claimed. Embedded in one batch: the
        alternative is a call per claim on every night, which turns a guard into a cost."""
        self._claims = list(claims)
        self._vectors = await embed.embed([c.text for c in self._claims]) if self._claims else []

    async def judge(self, candidate: Claim, embed: Embedder) -> Verdict:
        """Decide one candidate against everything already claimed.

        An allowed claim is NOT added to the gate — `remember` does that, and it is separate so
        a caller that judges a draft it then discards does not poison the comparison for the
        draft it keeps."""
        if not self._claims:
            return Verdict(allowed=True, reason="nothing claimed yet")
        [vector] = await embed.embed([candidate.text])
        best, best_score = None, -1.0
        for claim, prior in zip(self._claims, self._vectors, strict=True):
            score = _cosine(vector, prior)
            if score > best_score:
                best, best_score = claim, score
        assert best is not None
        if best_score < self.threshold:
            return Verdict(True, "not close to anything said before", best_score, best)
        # Close enough to be the same claim. The two exceptions, in order — supersession first
        # because it is the stronger signal: a changed view is development even when it cites
        # nothing new, while new evidence for an unchanged view is the weaker case.
        if candidate.stem == best.stem and candidate.object != best.object:
            return Verdict(True, "supersedes a prior claim", best_score, best)
        fresh = candidate.citations - best.citations
        if fresh:
            return Verdict(
                True, f"cites evidence the prior claim lacked: {sorted(fresh)}", best_score, best
            )
        return Verdict(False, "already said, with nothing new", best_score, best)

    def remember(self, claim: Claim, vector: list[float] | None = None) -> None:
        """Record a claim as said. Callers that already embedded it can pass the vector back
        rather than paying for it twice."""
        self._claims.append(claim)
        self._vectors.append(vector or [])

    async def remember_now(self, claim: Claim, embed: Embedder) -> None:
        [vector] = await embed.embed([claim.text])
        self.remember(claim, vector)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
