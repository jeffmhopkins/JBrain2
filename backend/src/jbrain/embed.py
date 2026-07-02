"""Embedding client and the embed_note job handler.

Embeddings come from the local TEI container (bge-small-en-v1.5, 384 dims) —
the same fakeable-protocol pattern the LLM adapter will use, so tests inject a
deterministic client and never touch the network. A dead/booting container
makes the job fail normally; queue backoff covers model-download windows.
"""

from collections.abc import Sequence
from typing import Any, Protocol

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()

# TEI handles long inputs via truncate=true; small batches keep the 1g
# container comfortably inside its memory cap.
EMBED_BATCH = 16


class EmbedClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input text, in order."""
        ...


class TeiEmbedClient:
    """text-embeddings-inference HTTP client (POST /embed)."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=60.0, transport=self._transport
        ) as client:
            for start in range(0, len(texts), EMBED_BATCH):
                batch = texts[start : start + EMBED_BATCH]
                resp = await client.post("/embed", json={"inputs": batch, "truncate": True})
                resp.raise_for_status()
                vectors.extend(resp.json())
        return vectors


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector input text for a bound parameter (`cast(:v AS vector)`).

    float() on every element means the output can only ever be numbers —
    injection-safe even before parameter binding.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class NoteEmbedder:
    """The embed_note job handler: fill NULL chunk embeddings for one note.

    Loads unembedded chunks at run time (chunk ids are not stable across
    re-ingestion, so the payload carries only the note id), and the UPDATE
    re-checks `embedding IS NULL` per chunk so a concurrent re-ingest can at
    worst no-op a row, never clobber a fresher chunk.
    """

    def __init__(self, maker: async_sessionmaker[AsyncSession], client: EmbedClient, model: str):
        self._maker = maker
        self._client = client
        self._model = model

    async def embed_note(self, payload: dict[str, Any]) -> None:
        note_id = str(payload["note_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, text FROM app.chunks"
                        " WHERE note_id = :nid AND embedding IS NULL ORDER BY seq"
                    ),
                    {"nid": note_id},
                )
            ).all()
        if not rows:
            log.info("embed.skipped", note_id=note_id, reason="nothing unembedded")
            return

        vectors = await self._client.embed([r.text for r in rows])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await session.execute(
                text(
                    "UPDATE app.chunks"
                    " SET embedding = cast(:emb AS vector), embedding_model = :model"
                    " WHERE id = :id AND embedding IS NULL"
                ),
                [
                    {"id": str(row.id), "emb": vector_literal(vec), "model": self._model}
                    for row, vec in zip(rows, vectors, strict=True)
                ],
            )
        log.info("embed.done", note_id=note_id, chunks=len(rows))


class PredicateEmbedder:
    """The sync_predicates job: keep the canonical_predicates index in step with
    the live schema registry (predicate canonicalization Phase 2). Upserts a row
    per registry-declared canonical (the registry is the source of truth, not a
    frozen migration snapshot), then fills missing/stale embeddings. Idempotent:
    re-running inserts nothing new and re-embeds only rows whose model changed.

    Seed rows a registry trim DEMOTED are left in place, not pruned: 0031 ships
    the table with no delete path at all (INSERT/UPDATE grants + policies only —
    canonicals are permanent by doctrine), so a prune would need a privilege-
    widening migration. The stale rows are inert suggestion-picker fodder, and a
    surviving new_predicate card can still resolve onto them (the alias FK needs
    the row) — exactly the guarded cases docs/ENTITY_GRAPH_REFOCUS_PLAN.md §3
    T1.3 protects, which is why it allows dropping the prune outright."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], client: EmbedClient, model: str):
        self._maker = maker
        self._client = client
        self._model = model

    async def sync_predicates(self, _payload: dict[str, Any]) -> None:
        from jbrain.analysis.predicates import registry_seed_rows

        seeds = registry_seed_rows()
        if not seeds:  # a degenerate/empty registry — never executemany on []
            log.warning("predicates.synced", upserted=0, embedded=0, reason="empty registry")
            return
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            # Upsert from the live registry: refresh descriptor + metadata on a
            # seed row that drifted, and NULL its embedding when the descriptor
            # changed so the backfill below re-embeds it (otherwise a registry
            # edit would leave the row matching a stale vector). origin='minted'
            # rows (Phase 3) are left untouched.
            await session.execute(
                text(
                    "INSERT INTO app.canonical_predicates"
                    " (canonical_name, descriptor, value_shape, kind, functional, origin)"
                    " VALUES (:canonical_name, :descriptor, :value_shape, :kind,"
                    " :functional, 'seed')"
                    " ON CONFLICT (canonical_name) DO UPDATE SET"
                    " descriptor = EXCLUDED.descriptor,"
                    " value_shape = EXCLUDED.value_shape,"
                    " kind = EXCLUDED.kind,"
                    " functional = EXCLUDED.functional,"
                    " embedding = CASE WHEN app.canonical_predicates.descriptor"
                    " IS DISTINCT FROM EXCLUDED.descriptor THEN NULL"
                    " ELSE app.canonical_predicates.embedding END,"
                    " embedding_model = CASE WHEN app.canonical_predicates.descriptor"
                    " IS DISTINCT FROM EXCLUDED.descriptor THEN NULL"
                    " ELSE app.canonical_predicates.embedding_model END"
                    " WHERE app.canonical_predicates.origin = 'seed'"
                ),
                [
                    {
                        "canonical_name": s.canonical_name,
                        "descriptor": s.descriptor,
                        "value_shape": s.value_shape,
                        "kind": s.kind,
                        "functional": s.functional,
                    }
                    for s in seeds
                ],
            )
            todo = (
                await session.execute(
                    text(
                        "SELECT canonical_name, descriptor FROM app.canonical_predicates"
                        " WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM :model"
                    ),
                    {"model": self._model},
                )
            ).all()
        if not todo:
            log.info("predicates.synced", upserted=len(seeds), embedded=0)
            return
        vectors = await self._client.embed([r.descriptor for r in todo])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await session.execute(
                text(
                    "UPDATE app.canonical_predicates"
                    " SET embedding = cast(:emb AS vector), embedding_model = :model"
                    " WHERE canonical_name = :name"
                ),
                [
                    {"name": r.canonical_name, "emb": vector_literal(vec), "model": self._model}
                    for r, vec in zip(todo, vectors, strict=True)
                ],
            )
        log.info("predicates.synced", upserted=len(seeds), embedded=len(todo))
