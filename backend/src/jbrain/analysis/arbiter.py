"""The arbiter's planning core — decides an IntegrationIntent's disposition.

This is the pure decision brain of Track A (plan §1, N3/N11): given the agent's
validated `IntegrationIntent` and the deterministic per-fact signals the arbiter
gathered, it partitions the intent into commit / review / reject — without
touching the DB. The DB executor (A1b) consumes the resulting `ArbiterPlan` and
performs the structural writes through the existing deterministic primitives
(_resolve validation, _upsert_fact, supersession.decide, the sweep).

Keeping the disposition logic pure here means the agent's non-determinism is
adjudicated by code that is fully unit-testable and reviewable; the agent never
decides its own commit-vs-review.

What the plan encodes:
- A FATAL structural violation (validate_intent) rejects the WHOLE intent — the
  note stays pending_integration, nothing is written (N5: no partial commit).
- A fact's weight (deterministic ceiling, self-confidence only lowers) decides
  active vs pending_review per kind (N11).
- A mention the agent left ambiguous, or a cross-subject attribution, forces its
  facts to review regardless of weight (N3 — never a silent wrong/leaky link).
- Merges and distinct-from proposals always route to review (N3 — the agent
  never folds identity).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from jbrain.analysis.intent import (
    EntityPairProposal,
    IntegrationIntent,
    IntentFact,
    IntentViolation,
    has_fatal,
    validate_intent,
)
from jbrain.analysis.weight import (
    CommitStatus,
    ConfidenceSignals,
    commit_status,
    effective_weight,
)

# When the executor couldn't supply signals for a fact, assume the most cautious
# reading: inferred, predicate unknown, would-overwrite. A safe default can only
# push a fact toward review, never silently commit it.
_CONSERVATIVE = ConfidenceSignals(surface_attested=False, predicate_known=False, is_supersede=True)


@dataclass(frozen=True)
class PlannedFact:
    fact: IntentFact
    weight: float
    status: CommitStatus
    # Non-empty when the status was forced to review by a resolution flag
    # (ambiguous / cross-subject), independent of the weight threshold.
    review_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArbiterPlan:
    rejected: bool  # a fatal structural violation held the whole intent
    fatal_violations: tuple[IntentViolation, ...]
    facts: tuple[PlannedFact, ...]
    # Identity proposals that always route to review (never auto-enacted).
    merge_proposals: tuple[EntityPairProposal, ...]
    distinct_proposals: tuple[EntityPairProposal, ...]

    @property
    def to_commit(self) -> tuple[PlannedFact, ...]:
        return tuple(f for f in self.facts if f.status == "active")

    @property
    def to_review(self) -> tuple[PlannedFact, ...]:
        return tuple(f for f in self.facts if f.status == "pending_review")


def plan_intent(
    intent: IntegrationIntent,
    signals: Mapping[int, ConfidenceSignals] | None = None,
) -> ArbiterPlan:
    """Partition an intent into commit / review / reject. `signals[i]` is the
    deterministic ConfidenceSignals for `intent.facts[i]` (executor-supplied;
    a missing entry is treated conservatively)."""
    sig = signals or {}

    violations = validate_intent(intent)
    if has_fatal(violations):
        return ArbiterPlan(
            rejected=True,
            fatal_violations=tuple(v for v in violations if v.severity == "fatal"),
            facts=(),
            merge_proposals=(),
            distinct_proposals=(),
        )

    # Mentions the agent could not pin to a single, same-subject identity force
    # their facts to review no matter how confident the value is.
    flagged: dict[str, str] = {}
    for r in intent.entity_resolutions:
        if r.mode == "ambiguous":
            flagged[r.mention_ref] = "ambiguous_mention"
        elif r.cross_subject:
            flagged[r.mention_ref] = "cross_subject_link"

    planned: list[PlannedFact] = []
    for i, fact in enumerate(intent.facts):
        weight = effective_weight(fact.self_confidence, sig.get(i, _CONSERVATIVE))
        status = commit_status(fact.kind, weight)
        # Order-preserving de-dup: a self-edge (same flagged mention as both
        # subject and object) must not repeat its reason.
        reasons = list(
            dict.fromkeys(
                flagged[ref]
                for ref in (fact.entity_ref, fact.object_entity_ref)
                if ref is not None and ref in flagged
            )
        )
        if reasons:
            status = "pending_review"
        elif status == "pending_review":
            # Held purely by the weight ceiling — record a machine-readable
            # reason so the inbox (A1b) needn't reconstruct it from weight+kind.
            reasons = ["below_threshold"]
        planned.append(
            PlannedFact(fact=fact, weight=weight, status=status, review_reasons=tuple(reasons))
        )

    return ArbiterPlan(
        rejected=False,
        fatal_violations=(),
        facts=tuple(planned),
        merge_proposals=tuple(intent.merge_proposals),
        distinct_proposals=tuple(intent.distinct_proposals),
    )
