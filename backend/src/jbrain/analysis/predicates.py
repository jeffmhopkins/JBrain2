"""The canonical-predicate index: descriptor synthesis, registry → seed rows,
the cosine nearest-neighbour query behind the held-fact predicate-suggestion
picker, and the one-shot boot sweep that retires the open new_predicate card
backlog the two-tier cutover orphaned.

Suggestions only — the embed-band DECISION (STRONG auto-merge / WEAK card) was
retired with the two-tier cutover (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md): the
Phase-4 calibration showed no cosine threshold separates drift spellings from
genuinely novel predicates, so an unknown predicate now commits raw and the
index's one job is ranking picker suggestions. The descriptor is the quality
lever: a bare predicate token embeds poorly (worksFor vs worksWith), so we
embed a synthesized definition + shape hint, not the token.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.queue import SYSTEM_CTX
from jbrain.schema import get_registry
from jbrain.schema.models import Predicate, SchemaRegistry, _norm_key

log = structlog.get_logger()

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_PRED_TOPK = 5


def _humanize(canonical_name: str) -> str:
    """`name.full` -> "name full", `bloodGlucose` -> "blood glucose"."""
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


async def alias_canonicals(session: AsyncSession, raws: Sequence[str]) -> dict[str, str]:
    """The durable raw->canonical aliases (Loop 3a, Wave 1) for a batch of raw predicates, keyed by
    `_norm_key(raw)`. A confirmed `map_to_existing`/rename wrote these so a resolved drift spelling
    collapses at canonicalize time instead of re-filing a card. Empty when none are aliased."""
    keys = list({_norm_key(r) for r in raws})
    if not keys:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT raw_norm, canonical_name FROM app.predicate_aliases"
                " WHERE raw_norm = ANY(:keys)"
            ),
            {"keys": keys},
        )
    ).all()
    return {r.raw_norm: r.canonical_name for r in rows}


async def record_predicate_alias(session: AsyncSession, raw: str, canonical: str) -> None:
    """Record a durable raw->canonical alias (idempotent) so the drift spelling collapses at
    canonicalize time on later runs. Written when a `new_predicate` card resolves to an existing
    canonical (the owner-approved resolution path); the FK guarantees `canonical` is a real
    canonical predicate."""
    # DO UPDATE (not DO NOTHING): a later re-resolution of the same raw to a DIFFERENT canonical
    # heals the stored facts onto the new target, so the durable alias must follow it too — else
    # the next canonicalize run would re-collapse the spelling to the stale (superseded) canonical.
    await session.execute(
        text(
            "INSERT INTO app.predicate_aliases (raw_norm, canonical_name)"
            " VALUES (:k, :c)"
            " ON CONFLICT (raw_norm) DO UPDATE SET canonical_name = excluded.canonical_name"
        ),
        {"k": _norm_key(raw), "c": canonical},
    )


async def delete_predicate_alias(session: AsyncSession, raw: str, canonical: str) -> None:
    """Drop a durable alias (idempotent), guarded on `canonical` so a later re-map's different
    alias isn't clobbered. Called when a `map_to_existing`/rename resolution is reopened, so the
    reopen fully reverses instead of leaving the spelling collapsing to the rejected canonical."""
    await session.execute(
        text("DELETE FROM app.predicate_aliases WHERE raw_norm = :k AND canonical_name = :c"),
        {"k": _norm_key(raw), "c": canonical},
    )


async def decide_predicates(
    session: AsyncSession,
    items: Sequence[tuple[str, str, str | None]],
    *,
    embedder: EmbedClient,
    k: int = _PRED_TOPK,
) -> list[tuple[tuple[str, float], ...]]:
    """Ranked canonical suggestions, top-k (canonical_name, similarity) per item
    (each item is (predicate, statement, kind)), for the held-fact predicate
    picker. A durable alias short-circuits to its owner-confirmed canonical
    (similarity 1.0 — a resolution, not a guess) with no embed; the rest
    cosine-match against the canonical index in ONE embed call."""
    if not items:
        return []
    aliases = await alias_canonicals(session, [p for p, _, _ in items])
    out: list[tuple[tuple[str, float], ...]] = []
    pending: list[tuple[int, str, str, str | None]] = []
    for idx, (p, s, kd) in enumerate(items):
        canonical = aliases.get(_norm_key(p))
        if canonical is not None:
            out.append(((canonical, 1.0),))
        else:
            out.append(())  # placeholder, filled after the embed
            pending.append((idx, p, s, kd))
    if pending:
        vectors = await embedder.embed([raw_descriptor(p, s, kd) for _, p, s, kd in pending])
        for (idx, _p, _s, _kd), v in zip(pending, vectors, strict=True):
            out[idx] = tuple(await nearest_predicates(session, v, k))
    return out


async def decide_predicate(
    session: AsyncSession,
    *,
    predicate: str,
    statement: str,
    kind: str | None,
    embedder: EmbedClient,
    k: int = _PRED_TOPK,
) -> tuple[tuple[str, float], ...]:
    """Single-predicate convenience over decide_predicates."""
    out = await decide_predicates(session, [(predicate, statement, kind)], embedder=embedder, k=k)
    return out[0]


# One-shot marker for the retirement sweep: an app.settings row (the
# constant-not-a-migration store) written in the SAME transaction as the
# sweep's DELETE. The worker calls the sweep at every boot, but it may only
# ever fire once per database: reopen_review returns a surviving card to
# status='open' (a deferred reopen even clears `resolution`, leaving the row
# indistinguishable from legacy backlog), so a second pass would silently
# destroy owner-touched cards.
_RETIRE_SWEEP_MARKER_KEY = "new_predicate_retire_swept"


async def retire_open_new_predicate_cards(maker: async_sessionmaker[AsyncSession]) -> int:
    """Boot sweep, one-shot per database: delete every OPEN `new_predicate`
    review card (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md §3 T1.3). The two-tier
    cutover stopped filing these — an unknown predicate now commits raw — so
    the open backlog is standing noise for a vocabulary that no longer grows.
    Open-only and kind-scoped: resolved/dismissed cards are human history and
    deferred cards were parked by the owner, so all survive (the re-extraction
    sweep's `statuses=('open',)` precedent).

    One-shot is enforced with a persisted app.settings marker, not by the
    backlog draining: the worker calls this every boot, and a card the owner
    reopens (resolved or un-parked deferred) returns to status='open' — a
    re-run of the DELETE would destroy it before the owner could act. The
    marker commits atomically with the DELETE; later boots skip. Runs on an
    RLS-scoped SYSTEM_CTX session like the pipeline; returns the count
    retired."""
    async with scoped_session(maker, SYSTEM_CTX) as session:
        already = (
            await session.execute(
                text("SELECT 1 FROM app.settings WHERE key = :k AND value = 'true'::jsonb"),
                {"k": _RETIRE_SWEEP_MARKER_KEY},
            )
        ).scalar_one_or_none()
        if already is not None:
            return 0
        # Pre-flight count = distinct unregistered spellings, exact by the old
        # filing rule (one open card per raw predicate).
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.review_items"
                    " WHERE kind = 'new_predicate' AND status = 'open'"
                )
            )
        ).scalar_one()
        retired: Sequence[str] = ()
        if count:
            log.info("predicates.retire_sweep", open_cards=count)
            retired = (
                (
                    await session.execute(
                        text(
                            "DELETE FROM app.review_items"
                            " WHERE kind = 'new_predicate' AND status = 'open'"
                            " RETURNING payload->>'predicate'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for spelling in retired:
                log.info("predicate.card_retired", predicate=spelling)
        # An empty backlog still counts as the one run — mark regardless, in
        # the delete's transaction (upsert: app.settings grants no DELETE, so
        # tests reset the marker by writing 'false').
        await session.execute(
            text(
                "INSERT INTO app.settings (key, value) VALUES (:k, 'true'::jsonb)"
                " ON CONFLICT (key) DO UPDATE"
                " SET value = excluded.value, updated_at = now()"
            ),
            {"k": _RETIRE_SWEEP_MARKER_KEY},
        )
        return len(retired)


async def nearest_predicates(
    session: AsyncSession, query_embedding: Sequence[float], k: int
) -> list[tuple[str, float]]:
    """The k canonical predicates closest to `query_embedding` by cosine
    similarity, strongest first — the suggestion ranking reads this. Global
    (predicates are not domain-scoped)."""
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
