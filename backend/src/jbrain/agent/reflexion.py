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
    entity_count: int = 0,
    mutated: bool,
    touched_sensitive: bool,
) -> bool:
    """The Loop-1 trigger (docs/ASSISTANT.md "Self-improvement loops"): a turn is
    worth verifying when it made a checkable claim or carried real-world
    consequence — it surfaced evidence (note sources *or* graph entities, both
    citation-bearing), it staged a mutation, or it actually touched sensitive data
    (a surfaced source/entity in the health|finance|location domains).

    Evidence counts entities, not only note sources: a turn answered straight from
    the entity graph (find_entity/read_entity → EntityRefs, zero NoteSources) is
    just as checkable as one answered from chunks, so it must be verified — and now
    grounds against the entity label+aliases rather than an empty corpus.

    The sensitive arm reads `touched_sensitive` (did a surfaced source/entity carry
    a sensitive domain?), NOT whether the session merely *holds* those scopes — Full
    Brain always holds general+health+finance+location, so a scope-membership test
    would make every Full Brain turn critique-worthy. Greetings and chit-chat — no
    evidence, no mutation, nothing sensitive touched — are never critique-worthy."""
    return source_count > 0 or entity_count > 0 or mutated or touched_sensitive


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
# all the signal it can use. Matched (not split) so the boundary's preceding word
# can be inspected to skip abbreviation periods ("St. Remigius" is one claim, not
# two) — see `claims_from`. Groups: (1) the word right before the terminator and
# (2) the terminator run, so the abbreviation check can require a period-only end.
_SENTENCE = re.compile(r"([A-Za-z]+)?([.!?]+)\s+|\n+")
# Abbreviations whose trailing period is not a sentence end. Lowercased; matched
# against the word preceding a "."-only terminator. A single capital letter is
# treated as an initial separately ("J. M. Hopkins" doesn't split).
_ABBREV = frozenset(
    [
        "st",
        "dr",
        "mr",
        "mrs",
        "ms",
        "sr",
        "jr",
        "prof",
        "rev",
        "gen",
        "sen",
        "vs",
        "etc",
        "al",
        "eg",
        "ie",
        "inc",
        "ltd",
        "co",
        "no",
        "vol",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
    ]
)


def significant_tokens(text: str) -> set[str]:
    """Lowercased content tokens (stopwords and 1-char tokens dropped)."""
    return {t for t in _WORD.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS}


def _is_abbrev_boundary(m: re.Match[str]) -> bool:
    """Whether a `_SENTENCE` match is a false boundary on an abbreviation period —
    a period-only terminator ("." not "!"/"?") whose preceding word is a known
    abbreviation ("St.", "Dr.") or a single-letter initial ("J.", "M."). Such a
    period does not end a sentence, so the splitter must not cut here (otherwise
    "St. Remigius" fragments and the inline flag anchors mid-name). A newline match
    has no groups and is always a real boundary."""
    word, term = m.group(1), m.group(2)
    if word is None or term != ".":
        return False
    return word.lower() in _ABBREV or len(word) == 1


def claims_from(answer: str) -> list[str]:
    """Split an answer into per-sentence claims for grounding (a coarse sentence
    split). Position-based (finditer, not re.split) so each claim is a verbatim
    substring of `answer` — the inline flag anchors by matching the rendered prose,
    so an internal abbreviation period ("St.") must be preserved. Only the trailing
    sentence terminator is excluded (as before). A boundary on an abbreviation
    period or a single-letter initial is skipped, so "St. Remigius" stays one claim.
    Blank fragments are dropped; a claim with no significant tokens passes grounding
    anyway, so this never needs to be precise."""
    claims: list[str] = []
    start = 0
    for m in _SENTENCE.finditer(answer):
        if _is_abbrev_boundary(m):
            continue
        # The claim is the verbatim run up to the terminator (group 2) for a .!?
        # boundary, or up to the newline run for a `\n+` match.
        end = m.start(2) if m.group(2) is not None else m.start()
        claim = answer[start:end].strip()
        if claim:
            claims.append(claim)
        start = m.end()
    tail = answer[start:].strip()
    if tail:
        claims.append(tail)
    return claims


# Pure social/filler tokens — a greeting or acknowledgement carries no checkable
# claim, so an answer made of only these is *not* substantive (the general-knowledge
# label must never fire on "hi" → "hello!" or a bare "ok, sure"). These are dropped
# only for the substantive-claim gate, never from grounding (where overlap on a
# filler word is harmless anyway).
_FILLER_TOKENS = frozenset(
    [
        "hi",
        "hey",
        "hello",
        "hiya",
        "yo",
        "ok",
        "okay",
        "sure",
        "yes",
        "yep",
        "yeah",
        "no",
        "nope",
        "thanks",
        "thank",
        "welcome",
        "please",
        "sorry",
        "bye",
        "goodbye",
        "cheers",
        "there",
        "here",
        "morning",
        "afternoon",
        "evening",
        "np",
        "yw",
    ]
)


def has_substantive_claim(answer: str) -> bool:
    """Whether an answer asserts something checkable — used to decide if a turn that
    retrieved NOTHING should carry the neutral "general knowledge" provenance label
    (docs/ASSISTANT.md). True when any claim sentence has a significant token that
    isn't pure social filler: an etymology ("Jeff is a short form of Jeffrey") is
    substantive, a greeting ("hello there", "ok, sure") is not. Pure (no model call,
    no I/O) so it stays as cheap and deterministic as the rest of the gate."""
    return any(significant_tokens(claim) - _FILLER_TOKENS for claim in claims_from(answer))


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


def _is_grounded(claim: str, sources: set[str], threshold: float) -> bool:
    """Whether one claim's significant tokens overlap the sources enough to count
    as grounded. A claim with no significant tokens (a greeting, a hedge) can't be
    ungrounded, so it grounds vacuously."""
    toks = significant_tokens(claim)
    return not toks or len(toks & sources) / len(toks) >= threshold


def ungrounded_claims(
    claims: Sequence[str], source_texts: Sequence[str], threshold: float = _GROUNDING_THRESHOLD
) -> list[str]:
    """The verbatim claim sentences that failed grounding — the structured twin of
    `verify_grounding`'s prose issues, so the PWA can anchor a flag against the
    exact answer sentence (docs/ASSISTANT.md "Self-improvement loops") instead of
    re-parsing the `"claim not grounded…: <sentence>"` issue prefix."""
    sources = significant_tokens(" ".join(source_texts))
    return [c for c in claims if not _is_grounded(c, sources, threshold)]


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
        if _is_grounded(claim, sources, threshold):
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
