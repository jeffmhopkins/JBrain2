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

- **Supersedes**: the claim lands in the same territory but reaches a DIFFERENT CONCLUSION —
  a high triple similarity with a LOW object similarity. It is not decided by string equality
  on the subject and predicate, which is what this gate tried first and what the box's own
  extractions killed: across six real posts making ONE claim, the model returned five
  spellings of the subject ("owner's prompt", "owner-given prompt", "owner's initial prompt",
  "owner prompt", "owner prompts") and four different predicates (CAUSES, REDUCES_TO, IS,
  CLAIMS_ABOUT). An exception gated on exact equality would have been unreachable code —
  silently, while looking like a safeguard.
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
# BELOW this cosine between two claims' OBJECTS, the second reached a different conclusion and
# the supersession exception fires. Provisional for the same reason.
DEFAULT_OBJECT_THRESHOLD = 0.75

_WORD = re.compile(r"[a-z0-9]+")
# A possessive is grammar, not content. Measured against the box's real extractions: the same
# subject came back as "owner's prompt", "owner-given prompt", "owner's initial prompt",
# "owner prompt" and "owner prompts" across six posts making ONE claim, and the apostrophe
# forms normalised to "owner s prompt" — a token that is not a word, in a subject that no
# longer matched any of its own spellings.
_POSSESSIVE = re.compile(r"['’]s\b|s['’](?=\s|$)")


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
    words = _WORD.findall(_POSSESSIVE.sub("", (raw or "").lower()))
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
        """Subject and predicate. Kept for logging and for the tests that pin normalisation —
        NOT used in the gate's decision, because real extractions do not agree on either well
        enough to compare by equality (see the module docstring)."""
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
    object_threshold: float = DEFAULT_OBJECT_THRESHOLD
    _claims: list[Claim] = field(default_factory=list)
    _vectors: list[list[float]] = field(default_factory=list)
    # The objects, embedded separately, so "same territory, different conclusion" is a
    # measurement rather than a string comparison.
    _objects: list[list[float]] = field(default_factory=list)

    async def load(self, claims: Sequence[Claim], embed: Embedder) -> None:
        """Seed the gate with what jmolt has already claimed. Embedded in one batch — the
        alternative is a call per claim on every night, which turns a guard into a cost. The
        triples and their objects go in the same request, for the same reason."""
        self._claims = list(claims)
        if not self._claims:
            self._vectors, self._objects = [], []
            return
        n = len(self._claims)
        vectors = await embed.embed(
            [c.text for c in self._claims] + [c.object for c in self._claims]
        )
        self._vectors, self._objects = vectors[:n], vectors[n:]

    async def judge(self, candidate: Claim, embed: Embedder) -> Verdict:
        """Decide one candidate against everything already claimed.

        An allowed claim is NOT added to the gate — `remember` does that, and it is separate so
        a caller that judges a draft it then discards does not poison the comparison for the
        draft it keeps."""
        if not self._claims:
            return Verdict(allowed=True, reason="nothing claimed yet")
        vector, object_vector = await embed.embed([candidate.text, candidate.object])
        best, best_score, best_i = None, -1.0, -1
        for i, (claim, prior) in enumerate(zip(self._claims, self._vectors, strict=True)):
            score = _cosine(vector, prior)
            if score > best_score:
                best, best_score, best_i = claim, score, i
        assert best is not None
        if best_score < self.threshold:
            return Verdict(True, "not close to anything said before", best_score, best)
        # Close enough to be the same claim. The two exceptions, in order — supersession first
        # because it is the stronger signal: a changed view is development even when it cites
        # nothing new, while new evidence for an unchanged view is the weaker case.
        object_score = _cosine(object_vector, self._objects[best_i])
        if object_score < self.object_threshold:
            return Verdict(
                True,
                f"same territory, different conclusion (objects {object_score:.2f} apart)",
                best_score,
                best,
            )
        fresh = candidate.citations - best.citations
        if fresh:
            return Verdict(
                True, f"cites evidence the prior claim lacked: {sorted(fresh)}", best_score, best
            )
        return Verdict(False, "already said, with nothing new", best_score, best)

    async def remember(self, claim: Claim, embed: Embedder) -> None:
        """Record a claim as said, so the rest of the night can repeat it.

        Deliberately takes the embedder rather than accepting a vector from the caller. The
        earlier signature let a caller hand back the vector it already had "to avoid paying
        twice", and a caller that passed none stored an EMPTY vector — a claim that could never
        match anything again, which is a gate silently switched off one claim at a time."""
        vector, object_vector = await embed.embed([claim.text, claim.object])
        self._claims.append(claim)
        self._vectors.append(vector)
        self._objects.append(object_vector)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
