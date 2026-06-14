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
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.embed import EmbedClient, vector_literal
from jbrain.schema import get_registry
from jbrain.schema.models import Predicate, SchemaRegistry

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Canonicalization bands (predicate canonicalization Phase 3, docs §3.1). Seeded
# at the entity-resolution values but NAMED separately: predicate descriptors are
# definition-vs-definition (a different distribution than name-vs-name), so
# Phase 4's eval recalibrates these without touching entity resolution.
_PRED_STRONG = 0.90  # >= this: canonicalize to the match automatically
_PRED_WEAK = 0.78  # [WEAK, STRONG): propose via a review card; below: cold (mint-proposal)
_PRED_TOPK = 5


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


def raw_descriptor(predicate: str, statement: str, kind: str | None = None) -> str:
    """Embed text for an INCOMING (unregistered) predicate: its humanized token
    plus the fact's statement (the model's intended meaning) and a kind hint.
    The sibling of predicate_descriptor, which needs a registry Predicate the
    incoming one isn't. The statement is the main signal — `worksFor` and
    `worksWith` diverge on it even when the tokens are lexically close."""
    parts = [_humanize(predicate), statement.strip(), f"({kind})" if kind else ""]
    return " ".join(p for p in parts if p)


@dataclass(frozen=True)
class PredicateDecision:
    """The canonicalization verdict for one unknown predicate (Phase 3 §3.1)."""

    band: Literal["strong", "weak", "cold"]
    canonical: str | None  # the STRONG match to rewrite to; None for weak/cold
    suggestions: tuple[tuple[str, float], ...]  # top-k (canonical_name, similarity)


def band_for(neighbors: tuple[tuple[str, float], ...]) -> PredicateDecision:
    """The band verdict for a predicate's nearest canonicals: STRONG (top
    >= _PRED_STRONG) canonicalizes; WEAK proposes the neighbours for review; cold
    (no/distant neighbour) is a mint proposal."""
    top = neighbors[0][1] if neighbors else 0.0
    if top >= _PRED_STRONG:
        return PredicateDecision("strong", neighbors[0][0], neighbors)
    return PredicateDecision("weak" if top >= _PRED_WEAK else "cold", None, neighbors)


async def decide_predicates(
    session: AsyncSession,
    items: Sequence[tuple[str, str, str | None]],
    *,
    embedder: EmbedClient,
    k: int = _PRED_TOPK,
) -> list[PredicateDecision]:
    """Cosine-match a batch of unknown predicates against the canonical index in
    ONE embed call (each item is (predicate, statement, kind)). The storage
    invariant holds for every band — the predicate name is never rejected."""
    if not items:
        return []
    vectors = await embedder.embed([raw_descriptor(p, s, kd) for p, s, kd in items])
    return [band_for(tuple(await nearest_predicates(session, v, k))) for v in vectors]


async def decide_predicate(
    session: AsyncSession,
    *,
    predicate: str,
    statement: str,
    kind: str | None,
    embedder: EmbedClient,
    k: int = _PRED_TOPK,
) -> PredicateDecision:
    """Single-predicate convenience over decide_predicates."""
    out = await decide_predicates(session, [(predicate, statement, kind)], embedder=embedder, k=k)
    return out[0]


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
