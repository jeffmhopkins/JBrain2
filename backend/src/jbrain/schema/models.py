"""Frozen value objects for the schema registry, plus the registry's read API.

Mirrors the `promptfile.PromptFile` style: immutable dataclasses, a single
`*Error(ValueError)` raised at load time so a malformed registry fails startup
rather than a live call. The registry is built by `jbrain.schema.loader`; this
module holds the shapes and the pure projection methods over them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

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

    `canonical_name` is the PREFERRED spelling (prompt digest + `renamed_from`
    normalization target), never a storage gate. `shape`/`range_type` name the
    target of a `structured`/`ref` value; `functional` marks supersede-on-change.
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
    advisory_required: bool = False
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

    def functional_predicates(self) -> tuple[str, ...]:
        """Canonical names eligible for supersession (the ANALYSIS functional
        allowlist, declared per predicate)."""
        return tuple(p.canonical_name for p in self.effective_predicates if p.functional)


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
    """The loaded registry: meta + facets + types, with the four projections.

    Reminder: `validate_value` is the ONLY rejection surface, and it rejects a
    malformed value_json shape — never a predicate name (docs/entity.md).
    """

    meta: Meta
    facets: dict[str, Facet]
    types: dict[str, EntityType]
    # normalized drift-spelling -> canonical predicate (the renamed_from attractor)
    normalization: dict[str, str]

    def type(self, type_id: str) -> EntityType:
        """The type by id; KeyError if unknown (callers know their type ids)."""
        return self.types[type_id]

    def normalize_predicate(self, predicate: str) -> str:
        """Rewrite a known drift spelling to its canonical predicate
        (`legalName` -> `name.legal`). An unknown predicate passes through
        unchanged: this is normalization toward a preferred name, NEVER a
        rejection (docs/entity.md invariant)."""
        return self.normalization.get(_norm_key(predicate), predicate)

    # -- consumer (a): the extraction prompt digest (advisory) ----------------

    def prompt_digest(self, type_id: str) -> str:
        """A compact, human-readable predicate vocabulary for `note.extract`.

        Advisory by contract: a *relaxation*, never a stricter grammar than
        storage accepts. `allow_open_predicates` only changes the closing tone.
        """
        t = self.type(type_id)
        ref = f" ({t.schema_org_ref})" if t.schema_org_ref else ""
        lines = [f"{t.name}{ref} — prefer these predicate names:"]
        for p in t.effective_predicates:
            enum = f" one of {list(p.enum_values)}" if p.enum_values else ""
            desc = f" — {p.description}" if p.description else ""
            lines.append(f"  {p.canonical_name}: {p.value_shape}{enum}{desc}")
        lines.append(
            "  (coin schema.org-style snake_case for anything else)"
            if t.allow_open_predicates
            else "  (these names are the schema; coin a new one only if truly needed)"
        )
        return "\n".join(lines)

    # -- consumer (b): the UI render config -----------------------------------

    def render_config(self) -> dict[str, Any]:
        """Per-type, per-predicate render hints: value_shape picks the widget,
        `display_name` the canonical-name projection. Replaces the bug where the
        UI fell back to rendering the whole statement sentence."""
        out: dict[str, Any] = {}
        for tid, t in self.types.items():
            out[tid] = {
                "display_name": list(t.display_name),
                "predicates": {
                    p.canonical_name: {
                        "value_shape": p.value_shape,
                        "kind": p.kind,
                        "shape": p.shape,
                        "range_type": p.range_type,
                    }
                    for p in t.effective_predicates
                },
            }
        return out

    # -- consumer (c): the resolution config ----------------------------------

    def resolution_config(self, type_id: str) -> dict[str, Any]:
        """Inputs the resolver/supersession already expect: the display-name
        projection, the alias-seeding predicates (feed the declared-name→alias
        path), and the functional set. Describes existing dispatch; it does not
        reconfigure identity (which stays mention-anchored)."""
        t = self.type(type_id)
        return {
            "display_name": list(t.display_name),
            "alias_seeding": list(t.alias_seeding_predicates),
            "functional": list(t.functional_predicates()),
        }

    # -- consumer (d): value-shape validation (the only rejection surface) -----

    def validate_value(self, type_id: str, canonical_name: str, value: Any) -> None:
        """Raise SchemaError iff `value` does not fit the predicate's declared
        `value_shape`. An UNKNOWN predicate is accepted silently — storage never
        gates predicate names (docs/entity.md invariant)."""
        pred = self.type(type_id).predicate(canonical_name)
        if pred is None:
            return  # open/long-tail predicate: no declared shape, nothing to check
        _check_shape(pred, value, self.meta)


# Map a declared value_shape to the Python the value_json must present.
_SCALAR_TYPES = (str, int, float, bool)


def _check_shape(pred: Predicate, value: Any, meta: Meta) -> None:
    shape = pred.value_shape
    name = pred.canonical_name
    if shape == "scalar":
        if not isinstance(value, _SCALAR_TYPES):
            raise SchemaError(f"{name}: expected a scalar, got {type(value).__name__}")
    elif shape == "text" or shape == "date":
        # date values reference a resolved temporal token, carried as a string.
        if not isinstance(value, str):
            raise SchemaError(f"{name}: expected a string, got {type(value).__name__}")
    elif shape == "enum":
        if value not in pred.enum_values:
            raise SchemaError(f"{name}: {value!r} not in {list(pred.enum_values)}")
    elif shape == "quantity":
        if not isinstance(value, dict) or "value" not in value or "unit" not in value:
            raise SchemaError(f"{name}: quantity needs {{value, unit}}")
        if not isinstance(value["value"], (int, float)):
            raise SchemaError(f"{name}: quantity.value must be numeric")
    elif shape == "ref":
        # An edge target: an entity id/name string (resolution links it later).
        if not isinstance(value, str):
            raise SchemaError(f"{name}: ref expects an entity id string")
    elif shape == "structured":
        if not isinstance(value, dict):
            raise SchemaError(f"{name}: structured value must be an object")
        if pred.shape is not None:
            allowed = set(meta.shapes.get(pred.shape, {}))
            extra = set(value) - allowed
            if extra:
                raise SchemaError(f"{name}: keys {sorted(extra)} not in shape {pred.shape!r}")
    else:  # pragma: no cover - loader rejects unknown shapes before this runs
        raise SchemaError(f"{name}: unhandled value_shape {shape!r}")


__all__ = [
    "EntityType",
    "Facet",
    "Meta",
    "Predicate",
    "SchemaError",
    "SchemaRegistry",
]
