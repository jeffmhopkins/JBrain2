"""Frozen value objects for the schema registry, plus the registry's read API.

Mirrors the `promptfile.PromptFile` style: immutable dataclasses, a single
`*Error(ValueError)` raised at load time so a malformed registry fails startup
rather than a live call. The registry is built by `jbrain.schema.loader`; this
module holds the value objects and the two wired read APIs (`normalize_predicate`,
`by_kind`) over them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NORM_SEP = re.compile(r"[\s_]+")


def _norm_key(spelling: str) -> str:
    """Case- and separator-insensitive key for matching predicate spellings, so
    `legal_name`, `legalName`, and `Legal Name` all collapse to one lookup."""
    return _NORM_SEP.sub("", spelling).casefold()


class SchemaError(ValueError):
    """A schema definition is malformed: an unknown facet/kind/value_shape, a
    cross-facet predicate collision, an unresolved ref/vocab/shape, or an enum
    predicate with no values. Raised at load time, never mid-pipeline."""


@dataclass(frozen=True)
class Predicate:
    """One property-graph edge spelling: `entity.canonical_name[.qualifier]`.

    `canonical_name` is the PREFERRED spelling (the `renamed_from` normalization
    target), never a storage gate. `shape`/`range_type` name the target of a
    `structured`/`ref` value; `functional` marks supersede-on-change.
    """

    canonical_name: str
    value_shape: str
    kind: str
    functional: bool = False
    qualifier_vocab: str | None = None
    enum_values: tuple[str, ...] = ()
    range_type: str | None = None
    shape: str | None = None
    renamed_from: tuple[str, ...] = ()
    schema_org_ref: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Facet:
    """A reusable property bundle (mixin) composed into types via `facets:`."""

    name: str
    description: str
    predicates: tuple[Predicate, ...]


@dataclass(frozen=True)
class EntityType:
    """One entity kind. `effective_predicates` is the rolled-down set (this
    type's facets + parent + own declarations), computed by the loader."""

    id: str
    name: str
    vehicle: str
    default_fact_kind: str
    allow_open_predicates: bool
    facets: tuple[str, ...]
    extends: str | None
    own_predicates: tuple[Predicate, ...]
    effective_predicates: tuple[Predicate, ...]
    alias_seeding_predicates: tuple[str, ...]
    display_name: tuple[str, ...]
    schema_org_ref: str | None = None
    description: str = ""

    def predicate(self, canonical_name: str) -> Predicate | None:
        """The effective predicate for a base canonical name, or None."""
        for p in self.effective_predicates:
            if p.canonical_name == canonical_name:
                return p
        return None


@dataclass(frozen=True)
class Meta:
    """`_meta.yaml`: the vocabularies every type draws on."""

    schema_version: int
    fact_kinds: frozenset[str]
    value_shapes: frozenset[str]
    shapes: dict[str, dict[str, str]]
    vocabs: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SchemaRegistry:
    """The loaded registry: meta + facets + types.

    Two consumers are wired today: predicate normalization (`normalize_predicate`,
    the `renamed_from` attractor) and the display projection (`by_kind` ->
    `display_name`, in `jbrain.analysis.canonical`). The YAML carries more
    schema (value_shapes, enum_values, alias-seeding, schema.org refs) that the
    loader validates and `docs/entity.md` documents, for projections that aren't
    built yet — deliberately NOT carried as speculative methods here.
    """

    meta: Meta
    facets: dict[str, Facet]
    types: dict[str, EntityType]
    # normalized drift-spelling -> canonical predicate (the renamed_from attractor)
    normalization: dict[str, str]
    # entities.kind -> type, keyed by BOTH the type id and its schema.org `name`,
    # so a "Person"/"person" or "Event"/"appointment" entity both resolve.
    by_kind: dict[str, EntityType]
    # _norm_key(canonical) of every predicate any type declares `functional` —
    # the registry-driven half of analysis.supersession.is_functional.
    functional_predicates: frozenset[str]
    # _norm_key(canonical) of every predicate any type declares — a cheap
    # kind-agnostic membership test so callers can skip work for the many
    # drift/unknown predicates no type defines.
    known_predicates: frozenset[str]
    # _norm_key(canonical) of every predicate declared with a qualifier_vocab —
    # the predicates whose qualifier a model may fold into the dotted path.
    qualifier_predicates: frozenset[str]

    def type(self, type_id: str) -> EntityType:
        """The type by id; KeyError if unknown (callers know their type ids)."""
        return self.types[type_id]

    def normalize_predicate(self, predicate: str) -> str:
        """Rewrite a known drift spelling to its canonical predicate
        (`legalName` -> `name.full`). An unknown predicate passes through
        unchanged: this is normalization toward a preferred name, NEVER a
        rejection (docs/entity.md invariant)."""
        return self.normalization.get(_norm_key(predicate), predicate)

    def decompose_predicate(self, predicate: str, qualifier: str) -> tuple[str, str]:
        """Normalize a predicate and recover a qualifier a model folded into its
        dotted path: ``("name.nickname.kids", "")`` → ``("name.nickname", "kids")``.

        Only splits when the incoming qualifier is empty, the full predicate is
        NOT itself declared, and stripping the trailing segment leaves a declared
        predicate that TAKES a qualifier (declares a ``qualifier_vocab``). So a
        genuine novel ``a.b``, an already-correct ``name.nickname`` + qualifier,
        and a dotted canonical like ``name.full`` are all left untouched — this is
        normalization toward the schema's shape, never a rejection."""
        norm = self.normalize_predicate(predicate)
        if qualifier or _norm_key(norm) in self.known_predicates:
            return norm, qualifier
        base, sep, segment = norm.rpartition(".")
        if sep and segment:
            base_norm = self.normalize_predicate(base)
            if _norm_key(base_norm) in self.qualifier_predicates:
                return base_norm, segment
        return norm, qualifier

    def is_functional(self, predicate: str) -> bool:
        """Whether a (canonical or drift) predicate is functional in the schema —
        at most one current value, so a new binding supersedes. Union semantics:
        functional if ANY type declares it so (mirrors the by-any-type proxy the
        arbiter uses for `predicate_known`)."""
        return _norm_key(self.normalize_predicate(predicate)) in self.functional_predicates

    def declares_predicate(self, predicate: str) -> bool:
        """Whether ANY type declares this (canonical or drift) predicate — a cheap
        registry-only check, no entity kind needed, so a caller can skip a
        kind lookup for the drift/unknown predicates no type defines."""
        return _norm_key(self.normalize_predicate(predicate)) in self.known_predicates

    def predicate_for_kind(self, kind: str, predicate: str) -> Predicate | None:
        """The declared `Predicate` for an entity `kind` (entities.kind, by id or
        schema.org name) and a canonical predicate name, or None when the kind is
        unknown or the type does not declare it. Never a storage gate — only the
        typed-value validator and projections read it."""
        entity_type = self.by_kind.get(kind)
        if entity_type is None:
            return None
        return entity_type.predicate(self.normalize_predicate(predicate))

    def enum_values_for(self, predicate: str) -> tuple[str, ...]:
        """The closed enum members of a (canonical or drift) predicate — kind-
        agnostic, for the review card's typed-value picker. Returns the members
        only when every type declaring this predicate as an enum agrees on them;
        `()` when they disagree (conservative — never guess a member set) or for
        a non-enum/unknown predicate. Union semantics mirror `is_functional`."""
        canonical = self.normalize_predicate(predicate)
        sets = {
            p.enum_values
            for t in self.types.values()
            if (p := t.predicate(canonical)) is not None
            and p.value_shape == "enum"
            and p.enum_values
        }
        return next(iter(sets)) if len(sets) == 1 else ()

    def coerce_value(self, pred: Predicate, value_json: dict | None) -> dict | None:
        """Normalize an enum value the model wrote as prose down to its declared
        member: ``{"value": "Female (inferred from 'wife')."}`` → ``{"value":
        "female"}``, and a bare ``"Female"`` → ``"female"``. So the stored datum —
        and the review card that renders it — reads as the member, not the agent's
        rationale or casing.

        CONSERVATIVE: only an enum predicate with a string value, and only when
        EXACTLY ONE member appears as a whole word (so ``male`` inside ``female``
        never mis-fires). Zero or 2+ matches pass through unchanged for
        `validate_value` to gate — coercion never invents or guesses a value."""
        if pred.value_shape != "enum" or not pred.enum_values:
            return value_json
        if not isinstance(value_json, dict):
            return value_json
        datum = value_json.get("value")
        if not isinstance(datum, str):
            return value_json
        lowered = datum.casefold()
        matched = [
            v for v in pred.enum_values if re.search(rf"\b{re.escape(v.casefold())}\b", lowered)
        ]
        if len(matched) != 1 or matched[0] == datum:
            return value_json
        return {**value_json, "value": matched[0]}

    def validate_value(
        self, pred: Predicate, value_json: dict | None, *, object_present: bool
    ) -> bool:
        """Whether `value_json` is acceptable for the predicate's declared
        `value_shape`. CONSERVATIVE: returns True unless the value clearly
        violates the shape, so a sound `value_json` is never dropped on a shape
        the registry under-specifies. `scalar`/`text`/`date` always pass (the
        datum lives in the statement or a temporal token, not `value_json`)."""
        if value_json is None:
            return True
        shape = pred.value_shape
        if shape == "ref":
            # An edge predicate: the value belongs on object_entity, not a scalar
            # payload. A literal value with no object is the classic "minted a
            # value as if it were the target" violation.
            return object_present
        if shape == "enum" and pred.enum_values:
            datum = value_json.get("value") if isinstance(value_json, dict) else None
            if datum is None:
                return True  # value carried in the statement, not value_json
            allowed = {v.casefold() for v in pred.enum_values}
            return str(datum).casefold() in allowed
        if shape == "quantity":
            # Tolerant of multi-field measurements ({systolic,diastolic,unit}):
            # only a non-dict (a bare scalar) is an unambiguous violation.
            return isinstance(value_json, dict)
        if shape == "structured" and pred.shape:
            allowed_keys = set(self.meta.shapes.get(pred.shape, {}))
            return isinstance(value_json, dict) and (
                not allowed_keys or set(value_json).issubset(allowed_keys)
            )
        return True


__all__ = [
    "EntityType",
    "Facet",
    "Meta",
    "Predicate",
    "SchemaError",
    "SchemaRegistry",
]
