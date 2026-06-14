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

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedMention,
    ExtractedTemporal,
    Extraction,
)
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
from jbrain.schema import get_registry

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


def compute_signals(
    intent: IntegrationIntent, chunk_texts: list[str]
) -> dict[int, ConfidenceSignals]:
    """Derive each fact's deterministic ConfidenceSignals (for the weight model,
    N11) from the intent + the note's chunk texts — no DB required:

    - surface_attested: the agent did NOT flag the fact inferred AND its attested
      surface text actually appears in the note. (Both must hold: an agent could
      claim a span it didn't read; requiring the surface to be present in the
      chunks is the deterministic check.)
    - predicate_known: the (already-normalized) predicate is a declared registry
      predicate, not a coined long-tail one.
    - is_supersede: the agent proposed superseding this fact's key. Derivable from
      the intent alone, so it's available at plan time (before entity resolution).
    """
    registry = get_registry()
    # Predicates are declared per entity-type; the entity's type isn't known until
    # the arbiter resolves it, so "known" here means declared by ANY type — a
    # sound global proxy for the minor unknown-predicate weight penalty.
    types = registry.types.values()
    haystack = "\n".join(chunk_texts)
    supersede_keys = {
        (s.entity_ref, s.predicate, s.qualifier)
        for s in intent.supersession_proposals
        if s.action in ("supersede", "conflict")
    }
    out: dict[int, ConfidenceSignals] = {}
    for i, fact in enumerate(intent.facts):
        surface_attested = (
            not fact.inferred
            and fact.attested_span is not None
            and fact.attested_span.surface in haystack
        )
        out[i] = ConfidenceSignals(
            surface_attested=surface_attested,
            predicate_known=any(t.predicate(fact.predicate) is not None for t in types),
            is_supersede=(fact.entity_ref, fact.predicate, fact.qualifier) in supersede_keys,
        )
    return out


def _to_extracted(fact: IntentFact, confidence: float) -> ExtractedFact:
    temporal = (
        ExtractedTemporal(
            phrase=fact.temporal.phrase,
            resolved_start=fact.temporal.resolved_start,
            resolved_end=fact.temporal.resolved_end,
            precision=fact.temporal.precision,
        )
        if fact.temporal is not None
        else None
    )
    # domain="" defers to the note's domain in _upsert_fact (`fact.domain or
    # note_domain`); the arbiter never overrides the firewall's floor/ratchet.
    return ExtractedFact(
        predicate=fact.predicate,
        qualifier=fact.qualifier,
        kind=fact.kind,
        statement=fact.statement,
        value_json=fact.value_json,
        assertion=fact.assertion,
        entity_ref=fact.entity_ref,
        object_entity_ref=fact.object_entity_ref,
        temporal=temporal,
        domain="",
        confidence=confidence,
        # The model's self-report rides alongside the plan weight so the
        # supersession guard can still hold a low-confidence overwrite (N11).
        self_confidence=fact.self_confidence,
    )


def plan_to_extraction(
    intent: IntegrationIntent,
    plan: ArbiterPlan,
    *,
    title: str = "",
    tags: list[str] | None = None,
    commit_only: bool = False,
) -> Extraction:
    """Bridge a (non-rejected) plan into the name-based `Extraction` the existing
    `_apply` consumes (plan §9, Option 1). Mentions and fact refs are keyed by
    `mention_ref`; each fact's `confidence` is its deterministic plan weight, not
    the model's self-report. title/tags come from the upstream extract step (the
    intent doesn't carry them). A1b-ii threads the agent's resolutions in as a
    name→entity override so `_resolve_entities` honors them.

    `commit_only` writes only active-eligible facts (`plan.to_commit`) — the
    A1b-ii-1 safety: a review-held fact (cross-subject, low weight) has no
    `_apply` path that respects its pending_review disposition yet, and some
    carry high weight `decide()` would otherwise commit, so they are excluded
    until A1b-ii-2 writes them as pending_review + a low_confidence_inference
    card. Mentions still cover every resolution (an entity may be mentioned
    without a committed fact)."""
    if plan.rejected:
        raise ValueError("cannot build an extraction from a rejected plan")
    source = plan.to_commit if commit_only else plan.facts
    # kind="Thing" for an existing resolution is harmless under Option 1: the
    # resolution-override (A1b-ii) supplies the entity directly, so kind_hint only
    # matters on the resolver fallback path, which an in-override ref never hits.
    mentions = [
        ExtractedMention(
            name=r.mention_ref,
            kind=r.new_kind or "Thing",
            surface_text=r.attested_span.surface if r.attested_span else r.mention_ref,
        )
        for r in intent.entity_resolutions
    ]
    facts = [_to_extracted(pf.fact, pf.weight) for pf in source]
    return Extraction(title=title, tags=list(tags or []), mentions=mentions, facts=facts, tokens=[])
