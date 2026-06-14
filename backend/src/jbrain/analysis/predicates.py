"""The canonical-predicate index: descriptor synthesis, registry → seed rows,
and the cosine nearest-neighbour query (predicate canonicalization Phase 2,
docs/PREDICATE_CANONICALIZATION.md §3).

Pure registry/SQL helpers — no embedding-canonicalization DECISION here (the
STRONG/WEAK bands are Phase 3). The descriptor is the quality lever: a bare
predicate token embeds poorly (worksFor vs worksWith), so we embed a synthesized
definition + shape hint, not the token.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.embed import vector_literal
from jbrain.schema import get_registry
from jbrain.schema.models import Predicate, SchemaRegistry

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _humanize(canonical_name: str) -> str:
    """`name.legal` -> "name legal", `bloodGlucose` -> "blood glucose"."""
    spaced = _CAMEL.sub(" ", canonical_name.replace(".", " "))
    return " ".join(spaced.split()).lower()


def predicate_descriptor(pred: Predicate) -> str:
    """The text we embed for a canonical predicate: its humanized name, its
    description when the registry gives one (often it does not), and a shape hint
    so the embedding captures whether the value is an edge, a quantity, a date,
    or one of an enum."""
    if pred.value_shape == "enum" and pred.enum_values:
        hint = f"(one of: {', '.join(pred.enum_values)})"
    elif pred.value_shape == "ref":
        hint = f"(link to a {pred.range_type or 'entity'})"
    else:
        hint = f"({pred.value_shape} value)"
    parts = [_humanize(pred.canonical_name), pred.description.strip(), hint]
    return " ".join(p for p in parts if p)


@dataclass(frozen=True)
class SeedRow:
    """One canonical_predicates row derived from the registry (no embedding)."""

    canonical_name: str
    descriptor: str
    value_shape: str
    kind: str
    functional: bool


def registry_seed_rows(registry: SchemaRegistry | None = None) -> list[SeedRow]:
    """Every canonical predicate the registry declares, deduped to the table's PK
    (a predicate appears under many types). A canonical declared with differing
    value_shape/kind across types — which the loader does NOT forbid — collapses
    deterministically: lexicographically-first (value_shape, kind), `functional`
    the union (any type that marks it functional)."""
    reg = registry or get_registry()
    by_name: dict[str, list[Predicate]] = {}
    for entity_type in reg.types.values():
        for pred in entity_type.effective_predicates:
            by_name.setdefault(pred.canonical_name, []).append(pred)
    rows: list[SeedRow] = []
    for name, preds in sorted(by_name.items()):
        winner = sorted(preds, key=lambda p: (p.value_shape, p.kind))[0]
        rows.append(
            SeedRow(
                canonical_name=name,
                descriptor=predicate_descriptor(winner),
                value_shape=winner.value_shape,
                kind=winner.kind,
                functional=any(p.functional for p in preds),
            )
        )
    return rows


async def nearest_predicates(
    session: AsyncSession, query_embedding: Sequence[float], k: int
) -> list[tuple[str, float]]:
    """The k canonical predicates closest to `query_embedding` by cosine
    similarity, strongest first. No band filtering — Phase 3's STRONG/WEAK
    decision reads this. Global (predicates are not domain-scoped)."""
    rows = (
        await session.execute(
            text(
                """
                SELECT canonical_name, 1 - (embedding <=> cast(:v AS vector)) AS sim
                FROM app.canonical_predicates
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> cast(:v AS vector)
                LIMIT :k
                """
            ),
            {"v": vector_literal(query_embedding), "k": k},
        )
    ).all()
    return [(r.canonical_name, float(r.sim)) for r in rows]
