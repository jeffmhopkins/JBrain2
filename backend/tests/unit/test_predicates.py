"""Pure tests for the canonical-predicate helpers (predicate canonicalization):
descriptor synthesis + registry seed-row collapse (Phase 2), and the embedding
band decision (Phase 3, nearest_predicates faked). No DB."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.analysis import predicates as pmod
from jbrain.analysis.predicates import (
    decide_predicate,
    predicate_descriptor,
    raw_descriptor,
    registry_seed_rows,
)
from jbrain.schema.models import EntityType, Meta, Predicate, SchemaRegistry

# nearest_predicates is monkeypatched in these tests, so the session is never
# touched — a typed null keeps the decide_predicate signature honest for pyright.
_NO_SESSION = cast(AsyncSession, None)


class _FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


def _patch_nearest(monkeypatch: pytest.MonkeyPatch, neighbors: list[tuple[str, float]]) -> None:
    async def fake(session: object, vec: object, k: int) -> list[tuple[str, float]]:
        return neighbors

    monkeypatch.setattr(pmod, "nearest_predicates", fake)


def test_raw_descriptor_includes_token_statement_and_kind() -> None:
    d = raw_descriptor("worksAtCompany", "Pat works at Globex", "relationship")
    assert "works at company" in d and "Globex" in d and "relationship" in d
    # Degrades to the bare humanized token when there's nothing else.
    assert raw_descriptor("x", "", None) == "x"


async def test_decide_predicate_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_nearest(monkeypatch, [("spouse", 0.95), ("knows", 0.60)])
    d = await decide_predicate(
        _NO_SESSION,
        predicate="marriedTo",
        statement="x",
        kind="relationship",
        embedder=_FakeEmbed(),
    )
    assert d.band == "strong" and d.canonical == "spouse"


async def test_decide_predicate_weak(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_nearest(monkeypatch, [("spouse", 0.82)])
    d = await decide_predicate(
        _NO_SESSION,
        predicate="partneredWith",
        statement="x",
        kind=None,
        embedder=_FakeEmbed(),
    )
    assert d.band == "weak" and d.canonical is None and d.suggestions[0][0] == "spouse"


async def test_decide_predicate_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_nearest(monkeypatch, [])
    d = await decide_predicate(
        _NO_SESSION,
        predicate="frobnicates",
        statement="x",
        kind=None,
        embedder=_FakeEmbed(),
    )
    assert d.band == "cold" and d.canonical is None and d.suggestions == ()


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
        canonical_name="name.full", value_shape="text", kind="state", description="legal full name"
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
        qualifier_predicates=frozenset(),
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
