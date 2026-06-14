"""The `IntegrationIntent` — the one seam between the Integrator agent and the
deterministic arbiter (docs/INTEGRATOR_PLAN.md §3).

The agent reads a note's stored `Extraction` plus the live graph and emits an
`IntegrationIntent`: its *judgment* about who is who, what is true, and what
supersedes what — as a proposal, never a write. The arbiter (the hardened
`_apply`) validates this object and performs every structural mutation itself.

Bounding the agent's non-determinism at this one value object is what keeps the
system testable and safe: the agent decides *semantics*; the deterministic core
decides *structure and the firewall* and owns commit (plan N1). So this module
deliberately carries NO chain pointers, NO offsets, and NO domain decisions —
the arbiter derives all three. An intent expresses intent; it never expresses a
committed fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from jbrain.analysis.extraction import ASSERTIONS, FACT_KINDS

# The supersession verdicts the agent may *propose*. The arbiter still wires the
# chain (sets `superseded_by`, orders by validity time, applies per-kind floors)
# — these only say what the agent thinks should happen, never how it is stored.
SUPERSESSION_ACTIONS = frozenset({"supersede", "conflict", "accumulate", "refresh"})

# How the agent resolved a mention's identity. "ambiguous" is a first-class,
# honest outcome — it routes to the review inbox rather than forcing a guess
# (plan N3: a wrong silent link is the one outcome no layer may produce).
ResolutionMode = Literal["existing", "new", "ambiguous"]


@dataclass(frozen=True)
class AttestedSpan:
    """Where in the note a value is stated. The agent names a chunk and the
    surface text; the arbiter RE-DERIVES the offsets from the chunk itself
    (plan I1/N10) — the agent never supplies offsets it could fabricate."""

    chunk_id: str
    surface: str


@dataclass(frozen=True)
class EntityResolution:
    """The agent's coreference judgment for one mention. `cross_subject` marks a
    link that attributes content to a different subject — always force-staged by
    the arbiter, never silently committed (plan N3, the cross-subject leak)."""

    mention_ref: str  # opaque id the intent's facts reference
    mode: ResolutionMode
    proposed_entity_id: str | None = None  # set iff mode == "existing"
    new_kind: str | None = None  # set iff mode == "new"
    new_name: str | None = None  # set iff mode == "new"
    cross_subject: bool = False
    attested_span: AttestedSpan | None = None
    rationale: str = ""


@dataclass(frozen=True)
class IntentTemporal:
    phrase: str | None
    resolved_start: datetime | None
    resolved_end: datetime | None
    precision: str


@dataclass(frozen=True)
class IntentFact:
    """A proposed `entity.predicate[.qualifier]` edge. `inferred=True` marks a
    fact the note does not literally state (a pronoun-resolved gender, an
    implied relationship): the arbiter caps its weight and routes it to review
    below threshold (plan N11). `object_entity_ref` names another mention_ref,
    never a minted entity — object binding stays the arbiter's job."""

    entity_ref: str  # a mention_ref from entity_resolutions
    predicate: str
    qualifier: str
    kind: str
    statement: str
    value_json: dict[str, Any] | None
    assertion: str
    object_entity_ref: str | None
    temporal: IntentTemporal | None
    attested_span: AttestedSpan | None
    self_confidence: float
    inferred: bool


@dataclass(frozen=True)
class SupersessionProposal:
    """The agent's read on whether a fact replaces existing history. The arbiter
    still owns the wiring; `action` is advisory and re-checked against the
    per-kind floor and validity-time ordering (plan N1/N7/N8)."""

    entity_ref: str
    predicate: str
    qualifier: str
    action: str  # one of SUPERSESSION_ACTIONS
    rationale: str = ""


@dataclass(frozen=True)
class EntityPairProposal:
    """A proposed merge or distinct-from edge between two EXISTING entities
    (these are entity ids, not mention_refs — you merge entities, not mentions).
    ALWAYS routed to review, regardless of confidence (plan N3) — the agent
    never folds identity."""

    entity_a_id: str
    entity_b_id: str
    rationale: str = ""


@dataclass(frozen=True)
class IntegrationIntent:
    note_id: str
    schema_version: int
    prompt_version: str
    integrator_version: str
    entity_resolutions: list[EntityResolution] = field(default_factory=list)
    facts: list[IntentFact] = field(default_factory=list)
    supersession_proposals: list[SupersessionProposal] = field(default_factory=list)
    merge_proposals: list[EntityPairProposal] = field(default_factory=list)
    distinct_proposals: list[EntityPairProposal] = field(default_factory=list)
    # Standalone span-anchored temporal tokens (mirroring Extraction.tokens) land
    # in Wave 1 with a dedicated IntentToken type; facts carry their own inline
    # temporal until then.


# --- Structural validation -------------------------------------------------
#
# A pure, DB-free pre-check the arbiter runs before trusting an intent. It
# catches malformed intent the model could emit; SEMANTIC validation (does the
# entity exist? is it in scope? is the supersession candidate domain-filtered?)
# is the arbiter's job against a live session (plan Wave-1 Track A). "fatal"
# violations reject the whole intent (the note stays pending_integration);
# "review" violations are normal — the offending item degrades to the inbox.

Severity = Literal["fatal", "review"]


@dataclass(frozen=True)
class IntentViolation:
    severity: Severity
    code: str
    detail: str


def validate_intent(intent: IntegrationIntent) -> list[IntentViolation]:
    """Structural-only checks; returns every violation found (empty = clean)."""
    out: list[IntentViolation] = []

    refs = {r.mention_ref for r in intent.entity_resolutions}
    for r in intent.entity_resolutions:
        if r.mode == "existing" and not r.proposed_entity_id:
            out.append(
                IntentViolation(
                    "fatal",
                    "resolution_missing_entity",
                    f"{r.mention_ref}: mode=existing without proposed_entity_id",
                )
            )
        if r.mode == "new" and not (r.new_kind and r.new_name):
            out.append(
                IntentViolation(
                    "fatal",
                    "resolution_missing_new",
                    f"{r.mention_ref}: mode=new without kind/name",
                )
            )
        if r.mode == "ambiguous":
            out.append(
                IntentViolation(
                    "review", "ambiguous_mention", f"{r.mention_ref}: agent could not disambiguate"
                )
            )
        if r.cross_subject:
            out.append(
                IntentViolation(
                    "review",
                    "cross_subject_link",
                    f"{r.mention_ref}: cross-subject attribution must be staged",
                )
            )
        # Fields that contradict the chosen mode (e.g. mode=new yet also naming an
        # existing entity) are incoherent intent — exactly what this pre-check
        # exists to surface rather than let the arbiter silently pick one.
        if r.mode != "existing" and r.proposed_entity_id:
            out.append(
                IntentViolation(
                    "review",
                    "resolution_conflicting_mode",
                    f"{r.mention_ref}: mode={r.mode} but carries proposed_entity_id",
                )
            )
        if r.mode != "new" and (r.new_kind or r.new_name):
            out.append(
                IntentViolation(
                    "review",
                    "resolution_conflicting_mode",
                    f"{r.mention_ref}: mode={r.mode} but carries new_kind/new_name",
                )
            )

    for i, f in enumerate(intent.facts):
        where = f"fact[{i}] {f.entity_ref}.{f.predicate}"
        if f.entity_ref not in refs:
            out.append(
                IntentViolation("fatal", "unknown_entity_ref", f"{where}: no such mention_ref")
            )
        if f.object_entity_ref is not None and f.object_entity_ref not in refs:
            out.append(
                IntentViolation(
                    "fatal",
                    "unknown_object_ref",
                    f"{where}: object {f.object_entity_ref} not in resolutions",
                )
            )
        if f.kind not in FACT_KINDS:
            out.append(IntentViolation("fatal", "bad_kind", f"{where}: kind={f.kind!r}"))
        if f.assertion not in ASSERTIONS:
            out.append(
                IntentViolation("fatal", "bad_assertion", f"{where}: assertion={f.assertion!r}")
            )
        if not 0.0 <= f.self_confidence <= 1.0:
            out.append(
                IntentViolation(
                    "fatal", "bad_confidence", f"{where}: self_confidence={f.self_confidence}"
                )
            )
        # A surface-attested fact must point at where the note states it; an
        # inferred fact need not (it gets capped + reviewed instead, N11).
        if not f.inferred and f.attested_span is None:
            out.append(
                IntentViolation(
                    "review",
                    "surface_fact_unanchored",
                    f"{where}: claims surface attestation but has no span",
                )
            )

    for sp in intent.supersession_proposals:
        if sp.action not in SUPERSESSION_ACTIONS:
            out.append(
                IntentViolation(
                    "fatal",
                    "bad_supersession_action",
                    f"{sp.entity_ref}.{sp.predicate}: action={sp.action!r}",
                )
            )

    # Merge/distinct pairs never auto-enact (they route to review), but a
    # self-pair or an empty id is structurally nonsensical, not a judgment call.
    for kind, pairs in (("merge", intent.merge_proposals), ("distinct", intent.distinct_proposals)):
        for p in pairs:
            if not p.entity_a_id or not p.entity_b_id:
                out.append(
                    IntentViolation(
                        "fatal", f"{kind}_empty_id", f"{kind} proposal with an empty id"
                    )
                )
            elif p.entity_a_id == p.entity_b_id:
                out.append(
                    IntentViolation(
                        "fatal",
                        f"{kind}_self_pair",
                        f"{kind} proposal of {p.entity_a_id} with itself",
                    )
                )

    return out


def has_fatal(violations: list[IntentViolation]) -> bool:
    return any(v.severity == "fatal" for v in violations)
