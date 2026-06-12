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

    def type(self, type_id: str) -> EntityType:
        """The type by id; KeyError if unknown (callers know their type ids)."""
        return self.types[type_id]

    def normalize_predicate(self, predicate: str) -> str:
        """Rewrite a known drift spelling to its canonical predicate
        (`legalName` -> `name.legal`). An unknown predicate passes through
        unchanged: this is normalization toward a preferred name, NEVER a
        rejection (docs/entity.md invariant)."""
        return self.normalization.get(_norm_key(predicate), predicate)


__all__ = [
    "EntityType",
    "Facet",
    "Meta",
    "Predicate",
    "SchemaError",
    "SchemaRegistry",
]
