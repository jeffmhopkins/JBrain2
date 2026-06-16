"""Reflexion (self-improvement Loop 1): bounded, ephemeral self-correction of a
turn before it is returned (docs/ASSISTANT.md "Self-improvement loops").

The gate is **mostly deterministic verifiers**, not an LLM judge — judges are
noisy, and a citation either resolves to an in-scope fact or it does not. A turn
flagged critique-worthy is verified; if it scores below perfect the loop may
re-run, but a retry is **adopted only when its verifier score strictly improves**,
and the whole thing is hard-capped at N=2 retries — so runaway is impossible and a
worse retry can never replace a better answer. Nothing here persists: Reflexion is
fully ephemeral (the gate: pure unit tests, no persistence touched).

An optional cheap LLM critic can break ties between equal deterministic scores,
but it is never the primary signal and is injected by the caller, never required.
"""

import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

PASS_SCORE = 1.0
MAX_RETRIES = 2

T = TypeVar("T")

# The domains whose content carries real-world consequence (the owner_scoped
# firewall — CLAUDE.md non-neg #3). A turn that touched any of these is worth
# verifying even if it surfaced no source card and staged no mutation.
SENSITIVE_SCOPES = frozenset(["health", "finance", "location"])


def critique_worthy(
    *,
    source_count: int,
    mutated: bool,
    scopes: Iterable[str],
) -> bool:
    """The Loop-1 trigger (docs/ASSISTANT.md "Self-improvement loops"): a turn is
    worth verifying when it made a checkable claim or carried real-world
    consequence — it surfaced sources (citation-bearing), it staged a mutation, or
    it ran in a sensitive scope (health|finance|location). Greetings and chit-chat
    — no sources, no mutation, only the general scope — are never critique-worthy,
    so the verifiers never run on them and the stream is untouched."""
    return source_count > 0 or mutated or bool(SENSITIVE_SCOPES & set(scopes))


# A claim is "grounded" when at least this fraction of its significant tokens
# appear in the retrieved sources — a deterministic proxy for "grounds in chunks".
_GROUNDING_THRESHOLD = 0.5

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "with",
        "from",
        "by",
        "your",
        "you",
        "i",
        "we",
        "they",
        "he",
        "she",
        "them",
        "his",
        "her",
        "their",
        "not",
        "no",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
    ]
)
_WORD = re.compile(r"[a-z0-9]+")
# Sentence boundaries for splitting an answer into checkable claims: a run of
# .!? (or a hard newline) ends one. A coarse split is deliberate — the grounding
# check is a token-overlap proxy, not a parser, so per-sentence granularity is
# all the signal it can use.
_SENTENCE = re.compile(r"[.!?]+\s+|\n+")


def significant_tokens(text: str) -> set[str]:
    """Lowercased content tokens (stopwords and 1-char tokens dropped)."""
    return {t for t in _WORD.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS}


def claims_from(answer: str) -> list[str]:
    """Split an answer into per-sentence claims for grounding (a coarse sentence
    split). Blank fragments are dropped; a claim with no significant tokens passes
    grounding anyway, so this never needs to be precise."""
    return [c.strip() for c in _SENTENCE.split(answer) if c.strip()]


@dataclass(frozen=True)
class VerificationResult:
    """A turn's verifier verdict: a 0..1 score and the concrete issues found.
    `score == PASS_SCORE` (and no issues) means nothing to correct."""

    score: float
    issues: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.score >= PASS_SCORE


def verify_citations(
    cited_fact_ids: Iterable[str], in_scope_fact_ids: Iterable[str]
) -> VerificationResult:
    """Cited facts must exist and be in the session's scope (RLS made them
    observable). No citations is a clean pass — nothing was claimed."""
    cited = list(cited_fact_ids)
    if not cited:
        return VerificationResult(PASS_SCORE, ())
    in_scope = set(in_scope_fact_ids)
    invalid = [c for c in cited if c not in in_scope]
    issues = tuple(f"cited fact not in scope: {c}" for c in invalid)
    return VerificationResult((len(cited) - len(invalid)) / len(cited), issues)


def verify_grounding(
    claims: Sequence[str], source_texts: Sequence[str], threshold: float = _GROUNDING_THRESHOLD
) -> VerificationResult:
    """Each claim should ground in the retrieved sources: a deterministic token-
    overlap proxy. A claim with no significant tokens (a greeting, a hedge) can't
    be ungrounded, so it passes."""
    if not claims:
        return VerificationResult(PASS_SCORE, ())
    sources = significant_tokens(" ".join(source_texts))
    grounded = 0
    issues: list[str] = []
    for claim in claims:
        toks = significant_tokens(claim)
        if not toks or len(toks & sources) / len(toks) >= threshold:
            grounded += 1
        else:
            issues.append(f"claim not grounded in retrieved sources: {claim}")
    return VerificationResult(grounded / len(claims), tuple(issues))


def verify_mutation(payload: dict, required_fields: Iterable[str]) -> VerificationResult:
    """A staged mutation must validate against its schema's required fields —
    all present and non-empty — before it can be proposed."""
    missing = [f for f in required_fields if not payload.get(f)]
    if not missing:
        return VerificationResult(PASS_SCORE, ())
    return VerificationResult(0.0, tuple(f"mutation missing required field: {f}" for f in missing))


def aggregate(results: Sequence[VerificationResult]) -> VerificationResult:
    """Combine verifier results: mean score, concatenated issues. No verifiers ran
    → a clean pass (there was nothing to check)."""
    if not results:
        return VerificationResult(PASS_SCORE, ())
    score = sum(r.score for r in results) / len(results)
    issues = tuple(issue for r in results for issue in r.issues)
    return VerificationResult(score, issues)


def strictly_improves(candidate: VerificationResult, incumbent: VerificationResult) -> bool:
    """The adoption rule: a retry replaces the incumbent only if it scores strictly
    higher. Equal-or-worse retries are discarded — a retry can never regress."""
    return candidate.score > incumbent.score


@dataclass(frozen=True)
class Reflection(Generic[T]):
    """The outcome of a reflexion pass: the best answer kept, its verdict, and how
    many retries it took. Ephemeral — the caller returns `answer` and drops the rest."""

    answer: T
    result: VerificationResult
    retries: int


async def reflect(
    produce: Callable[[], Awaitable[tuple[T, VerificationResult]]],
    *,
    max_retries: int = MAX_RETRIES,
    seed: tuple[T, VerificationResult] | None = None,
) -> Reflection[T]:
    """Run a turn, verify it, and re-run up to `max_retries` times — keeping a retry
    only when it strictly improves the verifier score. `produce` yields one
    (answer, verdict) per call; the loop owns the hard cap, so Reflexion can never
    spin and can never adopt a worse answer than it already has.

    `seed` supplies an already-produced first attempt (so a caller that produced it
    to decide whether to reflect at all does not pay for it twice); when omitted the
    controller produces the first attempt itself."""
    answer, result = seed if seed is not None else await produce()
    retries = 0
    while not result.passed and retries < max_retries:
        retries += 1
        candidate_answer, candidate_result = await produce()
        if strictly_improves(candidate_result, result):
            answer, result = candidate_answer, candidate_result
        else:
            # The retry didn't help; re-running the same context again is unlikely
            # to, so stop early and keep the best answer so far.
            break
    return Reflection(answer=answer, result=result, retries=retries)
