"""Pure tests for the canonical-predicate helpers (predicate canonicalization
Phase 2): descriptor synthesis and the registry -> seed-row collapse. No DB."""

from __future__ import annotations

from jbrain.analysis.predicates import predicate_descriptor, registry_seed_rows
from jbrain.schema.models import EntityType, Meta, Predicate, SchemaRegistry


def test_predicate_descriptor_humanizes_name_and_adds_shape_hint() -> None:
    qty = Predicate(canonical_name="bloodGlucose", value_shape="quantity", kind="measurement")
    d = predicate_descriptor(qty)
    assert "blood glucose" in d and "quantity" in d

    enum = Predicate(
        canonical_name="status", value_shape="enum", kind="state", enum_values=("active", "closed")
    )
    de = predicate_descriptor(enum)
    assert "active" in de and "closed" in de

    ref = Predicate(
        canonical_name="spouse", value_shape="ref", kind="relationship", range_type="person"
    )
    assert "person" in predicate_descriptor(ref)

    # A registry-provided description is woven in alongside the synthesized hint.
    described = Predicate(
        canonical_name="name.legal", value_shape="text", kind="state", description="legal full name"
    )
    assert "legal full name" in predicate_descriptor(described)


def _type(tid: str, preds: tuple[Predicate, ...]) -> EntityType:
    return EntityType(
        id=tid,
        name=tid.title(),
        vehicle="thing",
        default_fact_kind="attribute",
        allow_open_predicates=True,
        facets=(),
        extends=None,
        own_predicates=preds,
        effective_predicates=preds,
        alias_seeding_predicates=(),
        display_name=("name",),
    )


def _registry(types: list[EntityType]) -> SchemaRegistry:
    return SchemaRegistry(
        meta=Meta(1, frozenset(), frozenset(), {}, {}),
        facets={},
        types={t.id: t for t in types},
        normalization={},
        by_kind={},
        functional_predicates=frozenset(),
        known_predicates=frozenset(),
    )


def test_registry_seed_rows_dedups_canonical_and_unions_functional() -> None:
    # Two types declare 'x' with differing shape/kind (the loader does not forbid
    # this); seed_rows must collapse to ONE PK row, deterministic tie-break.
    pa = Predicate(canonical_name="x", value_shape="text", kind="state")
    pb = Predicate(canonical_name="x", value_shape="ref", kind="relationship", functional=True)
    rows = registry_seed_rows(_registry([_type("a", (pa,)), _type("b", (pb,))]))
    xs = [r for r in rows if r.canonical_name == "x"]
    assert len(xs) == 1
    # lexicographically-first (value_shape, kind): ('ref',...) < ('text',...).
    assert xs[0].value_shape == "ref"
    # functional is the union across declaring types.
    assert xs[0].functional is True
