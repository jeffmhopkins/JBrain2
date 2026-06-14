"""The weight model — deterministic confidence ceilings for integrated facts.

Plan N11: a fact's weight is NOT the model's self-reported number. The agent's
self-confidence is untrusted content (docs/ASSISTANT.md: content-derived
importance is untrusted and capped), so it may only ever *lower* a deterministic
ceiling computed from signals the arbiter can check itself — it can make the
system more cautious, never more permissive. The commit-vs-review decision reads
the ceiling-bounded weight, not the raw self-report.

This module is pure: the signals are passed in (the arbiter gathers them from
the graph/registry/span check). Per-kind supersession *floors* (a measurement or
event never auto-supersedes regardless of weight) live in `supersession.decide`
— "floor-wins": weight can gate a commit but can never promote a fact past a
supersession floor.

Thresholds are seeded conservative and meant to be tuned by review-inbox
rejection rate per inferred predicate (plan §8 open decision).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# A fact whose value is not surface-attested (inferred: a pronoun-resolved
# gender, an implied relationship) cannot exceed this ceiling — it must clear a
# kind threshold on its own merit or fall to review.
INFERRED_CEILING = 0.6
# An inferred fact that would OVERWRITE existing history is the dangerous case
# (silently rewriting a value the note never stated), so it is capped harder.
INFERRED_OVERWRITE_CEILING = 0.4
# A predicate the schema registry doesn't know is a weaker signal than a
# schema.org-canonical one.
UNKNOWN_PREDICATE_PENALTY = 0.1

# Commit-vs-review threshold per fact kind. Attributes (gender, birthday) are
# strictest — a wrong one is a bug, not news; preferences are loosest. Kinds not
# listed use DEFAULT_THRESHOLD. (measurement/event still can't auto-supersede —
# that floor is in supersession.decide, independent of this threshold.)
COMMIT_THRESHOLDS: dict[str, float] = {
    "attribute": 0.8,
    "relationship": 0.7,
    "state": 0.7,
    "measurement": 0.7,
    "event": 0.7,
    "preference": 0.5,
}
DEFAULT_THRESHOLD = 0.7

CommitStatus = Literal["active", "pending_review"]


@dataclass(frozen=True)
class ConfidenceSignals:
    """Deterministic, arbiter-checkable signals — never the model's opinion."""

    # The value's surface was verified at an attested span (plan I1). This is the
    # surface-attested vs inferred discriminator.
    surface_attested: bool
    # The predicate resolves in the schema registry (after normalization).
    predicate_known: bool
    # This fact would supersede an existing active head (vs filling an empty key).
    is_supersede: bool
    # Future (Wave-1 Track A): an `object_resolved` signal for relationship facts
    # (an inferred edge to an unresolved object should lower the ceiling). Add as
    # a defaulted field — the dataclass is frozen but a default keeps it
    # backward-compatible with existing callers.


def ceiling(signals: ConfidenceSignals) -> float:
    """The maximum weight these signals allow, in [0, 1]."""
    c = 1.0 if signals.surface_attested else INFERRED_CEILING
    if not signals.predicate_known:
        c -= UNKNOWN_PREDICATE_PENALTY
    if not signals.surface_attested and signals.is_supersede:
        c = min(c, INFERRED_OVERWRITE_CEILING)
    return max(0.0, min(1.0, c))


def effective_weight(self_confidence: float, signals: ConfidenceSignals) -> float:
    """The committed confidence (N11, refined by real-model calibration):

    - A SURFACE-ATTESTED fact (the note literally states it) gets its full
      ceiling. The note is the authority, not the agent's self-report — which is
      noisy run-to-run — so a stated fact is never dragged into review by the
      agent under-rating its own certainty.
    - An INFERRED fact's weight is the model's self-confidence bounded by the
      ceiling: it may only LOWER it, never inflate (the anti-inflation rule that
      keeps a confident guess from buying a commit)."""
    cap = ceiling(signals)
    if signals.surface_attested:
        return cap
    return max(0.0, min(self_confidence, cap))


def commit_status(kind: str, weight: float) -> CommitStatus:
    """Whether a fact at this weight commits active or holds for review."""
    threshold = COMMIT_THRESHOLDS.get(kind, DEFAULT_THRESHOLD)
    return "active" if weight >= threshold else "pending_review"


def assess(
    kind: str, self_confidence: float, signals: ConfidenceSignals
) -> tuple[float, CommitStatus]:
    """Convenience: the bounded weight and the commit decision in one call."""
    weight = effective_weight(self_confidence, signals)
    return weight, commit_status(kind, weight)
