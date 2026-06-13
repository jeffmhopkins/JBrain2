"""Span-keyed resolution pins — the convergence mechanism (plan N10).

The Integrator agent's judgment (which entity a mention is; the canonical
predicate for a key) is stochastic, so a re-run could silently re-decide and
fork a chain — the "silent flip" the design forbids. A resolution pin memoizes
that decision against the *text it was made about*, so a re-run reuses the
pinned decision as long as the underlying span is unchanged, and only
re-decides when the text actually changed.

The hard lessons from the data-integrity red team (A8) are baked in here:
- Key on the **occurrence index of the surface string**, never a raw char
  offset — inserting text above a span shifts offsets but not which occurrence
  of "Globex" this is, so the pin survives content-preserving edits.
- Two identical surfaces in one note ("Globex" in a header and a body) are
  disambiguated by occurrence index, never collapsed.
- **Never pin an empty/zero-width span** — those route to review instead
  (`build_pin` returns None), so a pronoun with no real surface can't seed a
  pin that cross-talks with another.

This module is pure (operates on chunk text). Two pieces stay in the arbiter
(Track A), needing DB state, and are NOT here: a human-`pinned` fact always
wins over a replayed pin, and a pin is invalidated when that flag flips.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

DecisionKind = Literal["identity", "predicate_key"]


def span_text_hash(surface: str) -> str:
    """Integrity digest of the spanned text (audit + a cheap mismatch check)."""
    return hashlib.sha256(surface.encode()).hexdigest()


def occurrence_index_at(text: str, surface: str, start: int) -> int | None:
    """Which 0-based occurrence of `surface` sits exactly at `start`, or None if
    `text` doesn't actually contain `surface` at that offset."""
    if not surface or text[start : start + len(surface)] != surface:
        return None
    # Count non-overlapping occurrences strictly before `start`.
    count = 0
    pos = text.find(surface)
    while pos != -1 and pos < start:
        count += 1
        pos = text.find(surface, pos + len(surface))
    return count


def locate_occurrence(text: str, surface: str, occurrence_index: int) -> int | None:
    """Start offset of the nth (0-based) occurrence of `surface`, or None if
    there are fewer than n+1 occurrences (the span moved away or was edited)."""
    if not surface or occurrence_index < 0:
        return None
    pos = text.find(surface)
    seen = 0
    while pos != -1:
        if seen == occurrence_index:
            return pos
        seen += 1
        pos = text.find(surface, pos + len(surface))
    return None


@dataclass(frozen=True)
class ResolutionPin:
    """A memoized agent decision, anchored to an occurrence of a surface string.
    Mirrors the eventual `resolution_pin` table; cascades on note delete (N15)."""

    note_id: str
    chunk_id: str
    decision_kind: DecisionKind
    occurrence_index: int
    surface: str
    span_text_hash: str
    # Exactly one of these is set, per decision_kind.
    entity_id: str | None = None
    normalized_predicate: str | None = None


def build_pin(
    *,
    note_id: str,
    chunk_id: str,
    decision_kind: DecisionKind,
    text: str,
    surface: str,
    start: int,
    entity_id: str | None = None,
    normalized_predicate: str | None = None,
) -> ResolutionPin | None:
    """Create a pin for a decision, or None if the span can't be pinned safely.

    Returns None when the surface is empty/zero-width or isn't actually present
    at `start` (N10: those decisions go to review rather than seed a pin)."""
    idx = occurrence_index_at(text, surface, start)
    if idx is None:
        return None
    return ResolutionPin(
        note_id=note_id,
        chunk_id=chunk_id,
        decision_kind=decision_kind,
        occurrence_index=idx,
        surface=surface,
        span_text_hash=span_text_hash(surface),
        entity_id=entity_id,
        normalized_predicate=normalized_predicate,
    )


def pin_holds(pin: ResolutionPin, current_text: str) -> bool:
    """Whether the pin's decision still applies to the current chunk text.

    True (reuse the pinned decision, override the agent's fresh judgment) iff the
    pin's occurrence of its surface is still locatable and its text matches;
    False (re-decide) when the span was edited or removed."""
    if locate_occurrence(current_text, pin.surface, pin.occurrence_index) is None:
        return False
    return span_text_hash(pin.surface) == pin.span_text_hash
