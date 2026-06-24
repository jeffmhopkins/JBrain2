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

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedMention,
    ExtractedTemporal,
    Extraction,
)
from jbrain.analysis.intent import (
    EntityPairProposal,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    IntentTemporal,
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
    # Owner-correction fact (Phase 6 §4): the executor force-supersedes + pins.
    correction: bool = False


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
    *,
    correction: bool = False,
) -> ArbiterPlan:
    """Partition an intent into commit / review / reject. `signals[i]` is the
    deterministic ConfidenceSignals for `intent.facts[i]` (executor-supplied;
    a missing entry is treated conservatively).

    `correction=True` (an owner-authored correction note, Phase 6 §4): each
    SURFACE-ATTESTED fact commits at FULL weight (the note is authoritative for what
    it literally states — skipping the ceiling so it never falls to review on
    threshold) and is marked a correction so the executor force-supersedes the
    current head + pins. An INFERRED fact in a correction note is NOT elevated (it
    follows the normal capped path) — a hallucinated value can't bypass the
    inferred-overwrite guard. Safety review flags (ambiguous mention, cross-subject
    link) STILL force review regardless."""
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
        signals_i = sig.get(i, _CONSERVATIVE)
        # A correction is authoritative only for what the note LITERALLY STATES (the note is the
        # authority, §4): an INFERRED (non-surface-attested) fact inside a correction note is NOT
        # allowed to bypass the weight ceiling or force-supersede a confident prior — a
        # hallucinated/pronoun-inferred value is the most destructive write, so it follows the
        # normal capped/review path. Only a surface-attested correction fact gets full weight +
        # force-supersede + pin.
        fact_correction = correction and signals_i.surface_attested
        weight = 1.0 if fact_correction else effective_weight(fact.self_confidence, signals_i)
        status: CommitStatus = "active" if fact_correction else commit_status(fact.kind, weight)
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
            PlannedFact(
                fact=fact,
                weight=weight,
                status=status,
                review_reasons=tuple(reasons),
                correction=fact_correction,
            )
        )

    return ArbiterPlan(
        rejected=False,
        fatal_violations=(),
        facts=tuple(planned),
        merge_proposals=tuple(intent.merge_proposals),
        distinct_proposals=tuple(intent.distinct_proposals),
    )


def _object_named(
    fact: IntentFact, res_by_ref: Mapping[str, EntityResolution], haystack: str
) -> bool:
    """An edge whose OBJECT entity is literally named in the note is surface-
    attested even when the model's own `attested_span` quote was imperfect or
    missing: the object's verbatim presence is independent evidence the stated
    edge exists. This is the deterministic backstop for the run-to-run flip the
    model produces on conjoined objects ("used to work for the US army and Oregon
    Lithoprint" — the second org's edge fumbles its quote and gets held below
    threshold while the first commits). Bounded to object edges and gated upstream
    by `not fact.inferred`, so a genuinely inferred edge — which makes no honest
    quote claim — is never promoted. The positive twin of weight.py's reserved
    `object_resolved` signal."""
    ref = fact.object_entity_ref
    if ref is None:
        return False
    res = res_by_ref.get(ref)
    if res is None:
        return False
    surface = (res.attested_span.surface if res.attested_span else None) or res.new_name
    return bool(surface) and _norm(surface) in haystack


def _relationship_object_named(fact: IntentFact, haystack: str) -> bool:
    """A relationship edge whose OBJECT the note names verbatim is surface-stated,
    even when the model flagged it inferred. The integrator under-flags ENUMERATED
    kinship ("daughters named Summer, Lydian, Harmony, Elora") as inferred for the
    non-first members — sinking them to the inferred ceiling and into review while
    the first commits — yet the note names each child outright. `object_entity_ref`
    is the extraction mention name, i.e. the note's own surface for the object, so
    its verbatim presence is deterministic grounding the model's self-assessment
    cannot override. This is the relationship twin of `_date_phrase_grounded`, and
    unlike `_object_named` it is NOT gated behind `not fact.inferred` — that gate is
    exactly what the under-flagging defeats. Scoped to relationship edges so a
    non-relationship inferred fact is never promoted by a bare name match."""
    ref = fact.object_entity_ref
    return fact.kind == "relationship" and ref is not None and _norm(ref) in haystack


def _norm(s: str) -> str:
    """Collapse whitespace and casing so an attestation check survives the model's
    quote drift (a reworded space, a capital, a smart quote) — the same string the
    note holds, just normalized."""
    return " ".join(s.split()).casefold()


def _token_present(token: str, haystack: str) -> bool:
    """The value appears in the note as a STANDALONE token, not merely as a
    substring. Containment (`token in haystack`) makes short values match by
    accident — `7` lands inside "$17", "7am", "level 7B" — which once forced a
    blunt length floor that wrongly rejected legitimate short values (a grade `7`,
    a blood type `A`). Word-edge anchors (not flanked by a word char) are the
    principled fix: a coincidental in-word hit fails the boundary, a genuinely
    stated short value passes. The lookarounds (not `\\b`) keep tokens whose own
    edge is non-word — `A+`, `B-` — matchable. Both args are already normalized."""
    if not token:
        return False
    return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", haystack) is not None


def _value_attested(value_json: dict | None, haystack: str) -> bool:
    """A fact whose stored VALUE is stated in the note (as a standalone token) is
    surface-attested even when the model's `attested_span` quote was paraphrased or
    omitted — the attribute twin of `_object_named` (an attribute carries no object
    to fall back on). `haystack` is already normalized. Gated upstream by
    `not fact.inferred`, so a guessed value the note never states is never promoted
    this way."""
    if not isinstance(value_json, dict):
        return False
    for v in value_json.values():
        if not isinstance(v, (str, int, float)):
            continue
        if _token_present(_norm(str(v)), haystack):
            return True
    return False


# Gendered kinship/role terms the note can state, mapped to the gender they
# DETERMINISTICALLY imply. Mirrors the set the integrator infers gender from (it
# never infers from the non-gendered partner/spouse/sibling/parent/child), plus the
# plurals and everyday variants a note actually uses.
_GENDER_TERMS: dict[str, frozenset[str]] = {
    "female": frozenset(
        {
            "daughter",
            "daughters",
            "wife",
            "mother",
            "mom",
            "sister",
            "sisters",
            "aunt",
            "aunts",
            "niece",
            "nieces",
            "grandmother",
            "granddaughter",
        }
    ),
    "male": frozenset(
        {
            "son",
            "sons",
            "husband",
            "father",
            "dad",
            "brother",
            "brothers",
            "uncle",
            "uncles",
            "nephew",
            "nephews",
            "grandfather",
            "grandson",
        }
    ),
}


def _gender_grounded(fact: IntentFact, haystack: str) -> bool:
    """A `gender` fact the note grounds with a gendered kinship/role term
    ("daughter" ⇒ female) is a DETERMINISTIC implication, not a speculative guess,
    so it is surface-attested even when the model flagged it inferred — the kinship
    twin of `_date_phrase_grounded` / `_relationship_object_named`. The term set
    mirrors what the integrator infers gender from (never the non-gendered
    partner/spouse/sibling/parent), so a fact grounded here genuinely is 1:1.

    The check is note-global like `_object_named`: a mixed-gender note ("a daughter
    and a son") could ground a model-misassigned gender against the wrong member —
    accepted, since the model rarely misassigns and the value is corrigible, and the
    common roster case (all same gender, or each gender named) is right. `haystack`
    is already normalized."""
    if fact.predicate != "gender" or not isinstance(fact.value_json, dict):
        return False
    value = fact.value_json.get("value")
    terms = _GENDER_TERMS.get(value.casefold()) if isinstance(value, str) else None
    return terms is not None and any(_token_present(t, haystack) for t in terms)


# The object-gender a kinship edge implies, by canonical predicate: the gendered
# noun for each side. A `children` edge to a child the note calls a "daughter"
# implies the child is female; spouse/parent/sibling likewise. Neutral terms (kid,
# child, partner, sibling, parent) imply nothing and are intentionally absent.
_KINSHIP_GENDER_TERMS: dict[str, dict[str, frozenset[str]]] = {
    "children": {
        "female": frozenset({"daughter", "daughters"}),
        "male": frozenset({"son", "sons"}),
    },
    "spouse": {"female": frozenset({"wife"}), "male": frozenset({"husband"})},
    "parent": {"female": frozenset({"mother", "mom"}), "male": frozenset({"father", "dad"})},
    "sibling": {
        "female": frozenset({"sister", "sisters"}),
        "male": frozenset({"brother", "brothers"}),
    },
}


def derive_kinship_gender(intent: IntegrationIntent, note_text: str) -> IntegrationIntent:
    """Emit the gender a kinship edge DETERMINISTICALLY implies for its object, so a
    roster the model captured as relationships but for which it omitted gender ("four
    daughters named …" → four children edges, no gender) still records each child's
    gender — the recall companion to `_gender_grounded`, which only weights a gender
    fact once it exists.

    For each kinship predicate, derive the object gender ONLY when the note uses that
    predicate's gendered term for exactly ONE gender (all daughters, or all sons); a
    mixed roster ("a daughter and a son") can't be associated to each object
    positionally here, so it is left to the model/review. An object that already
    carries a gender fact this note is left untouched. The derived fact is `inferred`
    (the note never types "female"), but `_gender_grounded` attests it, so it commits
    rather than landing in review."""
    registry = get_registry()
    haystack = _norm(note_text)
    implied: dict[str, str] = {}
    for canon, by_gender in _KINSHIP_GENDER_TERMS.items():
        present = {
            g for g, terms in by_gender.items() if any(_token_present(t, haystack) for t in terms)
        }
        if len(present) == 1:
            implied[canon] = next(iter(present))
    if not implied:
        return intent

    have_gender = {
        f.entity_ref for f in intent.facts if registry.normalize_predicate(f.predicate) == "gender"
    }
    added: list[IntentFact] = []
    seen: set[str] = set()
    for fact in intent.facts:
        gender = implied.get(registry.normalize_predicate(fact.predicate))
        obj = fact.object_entity_ref
        if gender is None or obj is None or obj in have_gender or obj in seen:
            continue
        seen.add(obj)
        added.append(
            IntentFact(
                entity_ref=obj,
                predicate="gender",
                qualifier="",
                kind="state",
                statement=f"{obj}'s gender is {gender}.",
                value_json={"value": gender},
                assertion="asserted",
                object_entity_ref=None,
                temporal=None,
                attested_span=None,
                self_confidence=1.0,
                inferred=True,
            )
        )
    if not added:
        return intent
    return replace(intent, facts=[*intent.facts, *added])


def recover_dropped_fields(intent: IntegrationIntent, extraction: Extraction) -> IntegrationIntent:
    """Backfill the object AND value the integrator drops when it re-types a fact.

    note.extract reliably emits `object_entity_ref` (Me.children -> Eli),
    `value_json` (grade {"value": "7th"}), and the `temporal` it resolved (an age
    phrase -> birthDate); the integrator non-deterministically omits them when it
    re-types the fact — orphaning the edge, blanking the value, or stripping the
    date phrase, after which the arbiter finds no named object / no datum / no
    grounding and holds the fact as inferred. Restore all three deterministically
    from the extraction — the source of truth for what the note states — keyed on
    the subject + canonical predicate. Then GUARANTEE every referenced entity
    (subject and object) carries a resolution: a backfilled object with no
    resolution would otherwise make apply_intent DROP the whole fact, so mint a
    provisional from the extraction mention's kind. Only fills gaps — an existing
    object/value/temporal/resolution (including a deliberate `ambiguous`) is never
    overridden."""
    registry = get_registry()
    # Objects can be MULTI-valued on one (subject, predicate): a set-valued
    # predicate (Me.children -> each kid) has one extraction edge per object, and
    # the integrator may drop the object on several of them at once. Keep EVERY
    # extraction object, in order, and hand them out positionally below — a single
    # value broadcast to all the object-less edges would turn N distinct edges
    # into N copies of the first, which then de-dup to one (the enumerated-kinship
    # collapse). value/temporal stay single-valued: they key on distinct subjects
    # (summer.name, lydian.name), not a shared one, so the first-wins is correct.
    ext_objs: dict[tuple[str, str], list[str]] = {}
    ext_val: dict[tuple[str, str], dict] = {}
    ext_temporal: dict[tuple[str, str], ExtractedTemporal] = {}
    for f in extraction.facts:
        key = (f.entity_ref, registry.normalize_predicate(f.predicate))
        if f.object_entity_ref:
            objs = ext_objs.setdefault(key, [])
            if f.object_entity_ref not in objs:
                objs.append(f.object_entity_ref)
        if isinstance(f.value_json, dict) and f.value_json:
            ext_val.setdefault(key, f.value_json)
        if f.temporal and f.temporal.phrase and f.temporal.resolved_start:
            ext_temporal.setdefault(key, f.temporal)
    mention_kind = {m.name: m.kind for m in extraction.mentions}
    resolved = {r.mention_ref for r in intent.entity_resolutions}
    # Objects still free to backfill per key: the extraction's, minus any an intent
    # edge already carries, so a kept edge's object is never handed to a sibling
    # too. The main loop consumes these in order as it meets each object-less edge.
    avail_objs: dict[tuple[str, str], list[str]] = {k: list(v) for k, v in ext_objs.items()}
    for kept in intent.facts:
        if kept.object_entity_ref:
            free = avail_objs.get((kept.entity_ref, registry.normalize_predicate(kept.predicate)))
            if free and kept.object_entity_ref in free:
                free.remove(kept.object_entity_ref)

    facts: list[IntentFact] = []
    added: list[EntityResolution] = []
    for fact in intent.facts:
        key = (fact.entity_ref, registry.normalize_predicate(fact.predicate))
        if fact.object_entity_ref is None:
            free = avail_objs.get(key)
            if free:
                fact = replace(fact, object_entity_ref=free.pop(0))
        if not isinstance(fact.value_json, dict) and key in ext_val:
            fact = replace(fact, value_json=ext_val[key])
        if fact.temporal is None and key in ext_temporal:
            t = ext_temporal[key]
            fact = replace(
                fact,
                temporal=IntentTemporal(
                    phrase=t.phrase,
                    resolved_start=t.resolved_start,
                    resolved_end=t.resolved_end,
                    precision=t.precision,
                ),
            )
        facts.append(fact)
        for ref in (fact.entity_ref, fact.object_entity_ref):
            if ref and ref not in resolved and ref in mention_kind:
                added.append(
                    EntityResolution(
                        mention_ref=ref, mode="new", new_kind=mention_kind[ref], new_name=ref
                    )
                )
                resolved.add(ref)
    if not added and facts == intent.facts:
        return intent
    return replace(intent, facts=facts, entity_resolutions=[*intent.entity_resolutions, *added])


def _date_phrase_grounded(fact: IntentFact, types: Iterable, haystack: str) -> bool:
    """A date-shape attribute is grounded when the temporal PHRASE it was resolved
    from appears in the note — even if the model flagged it inferred. An age ->
    birthDate derivation ("Eli, 12" -> born 2013) marks the fact inferred because
    the note never states the birthday, yet it is deterministic arithmetic over a
    STATED age, not a guess; holding one per family member per note is pure review
    noise. Scoped to date-shape predicates so a non-date inferred fact is never
    promoted by a mere timestamp, and `haystack` is already normalized."""
    if not (fact.temporal and fact.temporal.phrase and fact.temporal.resolved_start):
        return False
    is_date = any(
        (p := t.predicate(fact.predicate)) is not None and p.value_shape == "date" for t in types
    )
    return is_date and _norm(fact.temporal.phrase) in haystack


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
    haystack = _norm("\n".join(chunk_texts))
    res_by_ref = {r.mention_ref: r for r in intent.entity_resolutions}
    supersede_keys = {
        (s.entity_ref, s.predicate, s.qualifier)
        for s in intent.supersession_proposals
        if s.action in ("supersede", "conflict")
    }
    out: dict[int, ConfidenceSignals] = {}
    for i, fact in enumerate(intent.facts):
        # surface_attested holds when the model didn't flag the fact inferred AND the
        # note independently grounds it — by the model's (normalized) quote, the
        # object's name, or the stored VALUE appearing in the note. The last two are
        # deterministic backstops for the model's run-to-run quote drift, which
        # otherwise dumps a clearly-stated fact into review under the inferred ceiling.
        # _relationship_object_named is a further backstop that fires EVEN when the
        # model flagged the edge inferred (see its docstring) — the date-phrase twin.
        surface_attested = (
            (
                not fact.inferred
                and (
                    (
                        fact.attested_span is not None
                        and _norm(fact.attested_span.surface) in haystack
                    )
                    or _object_named(fact, res_by_ref, haystack)
                    or _value_attested(fact.value_json, haystack)
                )
            )
            or _date_phrase_grounded(fact, types, haystack)
            or _relationship_object_named(fact, haystack)
            or _gender_grounded(fact, haystack)
        )
        out[i] = ConfidenceSignals(
            surface_attested=surface_attested,
            predicate_known=any(t.predicate(fact.predicate) is not None for t in types),
            is_supersede=(fact.entity_ref, fact.predicate, fact.qualifier) in supersede_keys,
        )
    return out


def _to_extracted(
    fact: IntentFact, confidence: float, *, correction: bool = False
) -> ExtractedFact:
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
        correction=correction,
    )


def plan_to_extraction(
    intent: IntegrationIntent,
    plan: ArbiterPlan,
    *,
    title: str = "",
    tags: list[str] | None = None,
    commit_only: bool = False,
    dropped_facts: int = 0,
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
    without a committed fact).

    `dropped_facts` carries the per-note cap's tail-drop count from the upstream
    extract step forward onto the rebuilt Extraction. The intent/plan only ever
    see the already-capped fact list, so this count would otherwise reset to 0
    here and the pipeline would never file the `extraction_truncated` card for a
    clipped long note (W0)."""
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
    facts = [_to_extracted(pf.fact, pf.weight, correction=pf.correction) for pf in source]
    return Extraction(
        title=title,
        tags=list(tags or []),
        mentions=mentions,
        facts=facts,
        tokens=[],
        dropped_facts=dropped_facts,
    )
