"""Find the promises in what jmolt published, whether or not it remembers making them.

Promise extraction (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2). A commitment opened here
becomes an obligation, which the composer puts in front of tomorrow's sitting — so an agent
that said "I'll come back to this" gets asked about it by its own context rather than needing
to have remembered.

**Deterministic, and never a model call.** The obvious implementation is to ask the model
"did you promise anything?", and it is the wrong one for the reason the cold study gives
directly: never let the model judge its own voice or its own record. A mid-tier model shown
its own text and asked a question about it produces whatever continues that text. It is also
the exact failure this engine exists to fix — the shipped night narrates commitments it did
not make and forgets ones it did, and asking it to audit that is asking the unreliable witness
to check the transcript.

So this is patterns over the published string. That is a real limitation and worth being
honest about: it catches conventional English promises and misses oblique ones. The
consequence of a miss is a promise nobody tracks, which is the status quo; the consequence of
a false positive is an obligation jmolt did not make, which it can abandon in one move. That
asymmetry is why the patterns lean permissive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The subject line stored on the obligation, capped hard: it is a HANDLE the composer prints,
# and the verbatim sentence goes in the evidence table where it belongs.
MAX_SUBJECT = 120
# A promise made in passing inside a long post is still a promise, but a "sentence" 400
# characters long is a parse failure, not a commitment.
MAX_SENTENCE = 400

# First person, future-committing. Deliberately anchored on the SUBJECT being jmolt: "you
# should look into this" is advice to someone else, and an agent that opened an obligation
# from it would be taking on every suggestion it ever made.
_PROMISE = re.compile(
    r"""\b(
        i\s?['’]?ll                      # I'll
      | i\s+will
      | i\s+plan\s+to
      | i\s+intend\s+to
      | i\s+am\s+going\s+to
      | i\s?['’]?m\s+going\s+to
      | i\s+want\s+to\s+(?:come\s+back|follow\s+up|check|test|try|read|look)
      | i\s+should\s+(?:come\s+back|follow\s+up|check|test|try|read|look)
      | let\s+me\s+(?:come\s+back|follow\s+up|check|test|try|read|look)
      | remind\s+me\s+to
      | next\s+time\s+i
      | tomorrow\s+i
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Said about a promise, not as one. "I'll never" and "if I'll" are not commitments.
_NOT_A_PROMISE = re.compile(
    r"\b(i\s?['’]?ll\s+never|i\s+will\s+never|i\s+won\s?['’]?t|if\s+i\s?['’]?(?:ll|\s+will))\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class Promise:
    """One commitment found in published text."""

    subject: str
    quote: str


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]


def _subject_of(sentence: str) -> str:
    """A short handle for the promise, taken from the sentence itself.

    Not a summary — a truncation. A summary is a model call, or a heuristic pretending to be
    one, and either way it is prose about prose, which is what the ledger exists to avoid."""
    cleaned = re.sub(r"\s+", " ", sentence).strip(" \"'“”‘’")
    if len(cleaned) <= MAX_SUBJECT:
        return cleaned
    # Cut at a word boundary so the handle reads as a clipped phrase, not a broken one.
    cut = cleaned[:MAX_SUBJECT].rsplit(" ", 1)[0]
    return f"{cut}…"


def find_promises(text: str) -> list[Promise]:
    """Every commitment in one published post or comment, in the order they were made.

    De-duplicated by subject, because a post that says the same thing twice made one promise —
    and because two identical obligations would print as two lines in tomorrow's brief, which
    is the repetition this engine exists to stop showing itself."""
    found: list[Promise] = []
    seen: set[str] = set()
    for sentence in _sentences(text):
        if len(sentence) > MAX_SENTENCE:
            continue
        if not _PROMISE.search(sentence) or _NOT_A_PROMISE.search(sentence):
            continue
        subject = _subject_of(sentence)
        key = subject.lower()
        if subject and key not in seen:
            seen.add(key)
            found.append(Promise(subject=subject, quote=sentence))
    return found
