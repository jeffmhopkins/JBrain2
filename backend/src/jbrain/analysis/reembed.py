"""The `reembed_stale` engine action (Phase-6 follow-on; docs/HYGIENE_SWEEPS_PLAN.md).

Nightly maintenance: re-embed the embedded rows whose `embedding_model` is stale (or whose
embedding is NULL) after an embed-model change — the rows that have NO existing re-embed
path. `wiki_index` already re-embeds via `wiki_reindex` and `canonical_predicates` via
`sync_predicates`, so this covers the gap: **skills** (description+body) and **entities**
(summary, when one exists). Uses the local embed container, not the LLM router, so it spends
no LLM tokens and needs no self-improvement budget; it is mechanical, idempotent
(`embedding_model IS DISTINCT FROM :model` self-clears as rows are updated), and bounded per
run so a big post-upgrade backlog spreads across nights. Runs under SYSTEM_CTX; the schedule
ships disabled and is Ops-fireable.
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

# Per-target, per-run cap: a large re-embed (a model swap touching every row) spreads over
# nights rather than embedding thousands in one sweep. The next run continues from what's
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
    description="Re-embed skills/entities whose embedding_model is stale after a model change.",
)


@dataclass(frozen=True)
class _Target:
    """One embedded table: how to find stale rows (id + the text to embed) and where to
    write the vector. `select` must yield `(id, src)`; `update` binds `:id`, `:emb`, `:model`."""

    name: str
    select: str
    update: str


_TARGETS = (
    _Target(
        name="skills",
        select=(
            "SELECT id::text AS id,"
            " coalesce(description, '') || E'\n' || coalesce(body, '') AS src"
            " FROM app.skills"
            " WHERE (embedding IS NULL OR embedding_model IS DISTINCT FROM :model)"
            "   AND btrim(coalesce(description, '') || coalesce(body, '')) <> ''"
            " ORDER BY id LIMIT :limit"
        ),
        update=(
            "UPDATE app.skills SET embedding = cast(:emb AS vector), embedding_model = :model"
            " WHERE id = cast(:id AS uuid)"
        ),
    ),
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
