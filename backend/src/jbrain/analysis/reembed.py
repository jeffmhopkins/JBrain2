"""The `reembed_stale` engine action (Phase-6 follow-on; docs/archive/HYGIENE_SWEEPS_PLAN.md).

Nightly maintenance: re-embed the rows whose `embedding_model` is stale after an
embed-model change — the rows that have NO existing re-embed path (and, where nothing
faster fills them, the rows whose embedding is still NULL). `wiki_index` already
re-embeds via `wiki_reindex` and `canonical_predicates` via `sync_predicates`, so this
covers the gap: **entities** and **external_sources** (summary, when one exists),
**external_source_chunks**, and **app.chunks**. Uses the local embed container, not the
LLM router, so it spends no LLM tokens and needs no self-improvement budget; it is
mechanical, idempotent (`embedding_model IS DISTINCT FROM :model` self-clears as rows
are updated), and bounded per run so a big post-upgrade backlog spreads across nights.
Runs under SYSTEM_CTX — the same all-domains context ingest and `embed_note` already
embed under, so it widens nobody's scope. Terminal-free (CLAUDE.md #10): 0066 seeds the
SCHEDULE disabled but the TRIGGER `manual` and enabled, so Ops → Automations can fire it
on demand today (`POST /ops/triggers/{id}/run`) and its toggle arms the nightly run.

`app.chunks` joined the list when `jbrain.ingest.carryover` landed. Before it, every
re-ingest destroyed and rebuilt a note's chunk rows, so any edit handed each chunk a fresh
NULL embedding and `embed_note` re-embedded it under the current model — a note touched
after a model swap healed itself. Carry-over keeps a byte-identical chunk's ROW (that is
the point: `wiki_citations` and `entity_mentions` cascade off it), and with the row its
old-model vector; `embed_note` selects `WHERE embedding IS NULL`, so nothing revisits it.
Without this target a model swap would leave `chunks_embedding_idx` holding mixed-model
vectors that only a genuinely rewritten body ever cleared, with no PWA or debug path to
fix it — precisely the shape CLAUDE.md #10 exists to stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.queue import SYSTEM_CTX
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

# The unit of work: one SELECT, one embed call, one committed UPDATE. A target may spend
# several of these per run (`_Target.passes`), but never more in one transaction, so a
# failed embed loses at most this many rows of progress. The next run continues from what's
# still stale (the WHERE self-advances), so it converges and is idempotent at the tail.
_BATCH = 256

REEMBED_SPEC = ActionSpec(
    name="reembed_stale",
    version=1,
    handler="reembed_stale",
    domain_optional=True,
    mutating=True,  # writes embedding + embedding_model
    cost_class="standard",  # local embed container, no LLM router
    dedup_key_expr=None,
    description="Re-embed rows whose embedding_model is stale after a model change.",
    category="maintenance",
)


@dataclass(frozen=True)
class _Target:
    """One embedded table: how to find stale rows (id + the text to embed) and where to
    write the vector. `select` must yield `(id, src)`; `update` binds `:id`, `:emb`, `:model`."""

    name: str
    select: str
    update: str
    # How many `_BATCH`-sized passes this target may spend per run. One is right for a
    # table holding hundreds of rows; `app.chunks` holds an order of magnitude more than
    # all the others together, and at one pass a nightly sweep would take months of nights
    # to drain a model swap. Each pass still commits alone, so a wide target costs nothing
    # in blast radius — only in how long one run occupies the (CPU-only) embed container.
    passes: int = 1


_TARGETS = (
    _Target(
        name="entities",
        select=(
            "SELECT id::text AS id, summary AS src FROM app.entities"
            " WHERE summary IS NOT NULL AND btrim(summary) <> ''"
            "   AND (summary_embedding IS NULL OR embedding_model IS DISTINCT FROM :model)"
            " ORDER BY id LIMIT :limit"
        ),
        update=(
            "UPDATE app.entities"
            " SET summary_embedding = cast(:emb AS vector), embedding_model = :model"
            " WHERE id = cast(:id AS uuid)"
        ),
    ),
    # The external-source corpus: passage chunks + the source-level summary vector. Their
    # only other embed path is embed_external_source on ingest, so a model swap re-embeds
    # them here (like entities).
    _Target(
        name="external_source_chunks",
        select=(
            "SELECT id::text AS id, text AS src FROM app.external_source_chunks"
            " WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM :model"
            " ORDER BY id LIMIT :limit"
        ),
        update=(
            "UPDATE app.external_source_chunks"
            " SET embedding = cast(:emb AS vector), embedding_model = :model"
            " WHERE id = cast(:id AS uuid)"
        ),
    ),
    _Target(
        name="external_sources",
        select=(
            "SELECT id::text AS id, summary AS src FROM app.external_sources"
            " WHERE summary IS NOT NULL AND btrim(summary) <> ''"
            "   AND (summary_embedding IS NULL OR embedding_model IS DISTINCT FROM :model)"
            " ORDER BY id LIMIT :limit"
        ),
        update=(
            "UPDATE app.external_sources"
            " SET summary_embedding = cast(:emb AS vector), embedding_model = :model"
            " WHERE id = cast(:id AS uuid)"
        ),
    ),
    # Note chunks. Last, so a failure on the biggest table cannot starve the small ones.
    #
    # Deliberately NOT `embedding IS NULL` like the targets above: `app.chunks` is the one
    # embedded table that already HAS a NULL-embedding healer — `reconcile_unembedded_notes`
    # re-enqueues `embed_note` every 300s — and sweeping NULLs here would only race a path
    # that is already 288x faster. What has no other healer is the opposite row: a chunk
    # that IS embedded, under a model that is no longer ours. See the module docstring.
    _Target(
        name="chunks",
        select=(
            "SELECT id::text AS id, text AS src FROM app.chunks"
            " WHERE embedding IS NOT NULL AND embedding_model IS DISTINCT FROM :model"
            " ORDER BY id LIMIT :limit"
        ),
        update=(
            "UPDATE app.chunks"
            " SET embedding = cast(:emb AS vector), embedding_model = :model"
            " WHERE id = cast(:id AS uuid)"
        ),
        passes=8,
    ),
)


class ReembedAction:
    """For each target: fetch a bounded batch of stale rows → embed their text via the
    local container → write the vector + stamp the current model."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        embedder: EmbedClient,
        embedding_model: str,
        batch: int = _BATCH,
        ctx: Any = SYSTEM_CTX,
    ):
        self._maker = maker
        self._embedder = embedder
        self._model = embedding_model
        self._batch = batch
        self._ctx = ctx

    async def run(self, _payload: dict[str, Any]) -> None:
        for target in _TARGETS:
            embedded = await self._reembed_one(target)
            if embedded:
                log.info("reembed_stale_swept", target=target.name, embedded=embedded)

    async def _reembed_one(self, target: _Target) -> int:
        """Spend up to `target.passes` batches, stopping early once the target is drained
        (a short batch means the stale set is exhausted, so another SELECT would return 0)."""
        total = 0
        for _ in range(target.passes):
            done = await self._reembed_batch(target)
            total += done
            if done < self._batch:
                break
        return total

    async def _reembed_batch(self, target: _Target) -> int:
        async with scoped_session(self._maker, self._ctx) as session:
            rows = (
                await session.execute(
                    text(target.select), {"model": self._model, "limit": self._batch}
                )
            ).all()
        if not rows:
            return 0
        vectors = await self._embedder.embed([r.src for r in rows])
        async with scoped_session(self._maker, self._ctx) as session:
            await session.execute(
                text(target.update),
                [
                    {"id": r.id, "emb": vector_literal(vec), "model": self._model}
                    for r, vec in zip(rows, vectors, strict=True)
                ],
            )
        return len(rows)


def reembed_handler(
    maker: async_sessionmaker[AsyncSession], *, embedder: EmbedClient, embedding_model: str
) -> Any:
    """Worker dispatch entry for `reembed_stale` (payload-only Handler)."""
    action = ReembedAction(maker, embedder=embedder, embedding_model=embedding_model)
    return action.run
