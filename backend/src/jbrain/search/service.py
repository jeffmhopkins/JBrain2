"""Hybrid search: dense + FTS legs fused with Reciprocal Rank Fusion.

Fusion runs in Python — at personal scale 40+40 candidates cost nothing, and
pure-function fusion is unit-testable without Postgres. Degraded mode (embed
container unreachable) is a feature, not an error: keyword results still
return, flagged so the UI can show its amber banner.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import structlog

from jbrain.db.session import SessionContext
from jbrain.embed import EmbedClient

log = structlog.get_logger()

RRF_K = 60
LEG_LIMIT = 40
SNIPPET_CHARS = 240
PREVIEW_CHARS = 160

Match = Literal["semantic", "keyword", "both"]


@dataclass(frozen=True)
class ChunkHit:
    """One candidate chunk from either leg, already joined to its note."""

    chunk_id: str
    note_id: str
    text: str
    source_kind: str
    source_anchor: str | None
    domain: str
    destination: str | None
    created_at: datetime
    body: str
    attachment_count: int
    headline: str | None = None  # ts_headline, FTS leg only


@dataclass(frozen=True)
class SearchResult:
    note_id: str
    chunk_id: str
    snippet: str
    match: Match
    score: float
    domain: str
    destination: str | None
    created_at: datetime
    body_preview: str
    attachment_count: int
    source_kind: str
    source_anchor: str | None


@dataclass(frozen=True)
class SearchResponse:
    degraded: bool
    results: list[SearchResult]


class SearchRepo(Protocol):
    async def dense_search(
        self, ctx: SessionContext, qvec: list[float], domain: str | None, limit: int
    ) -> list[ChunkHit]:
        """Top chunks by cosine distance; only embedded, non-deleted notes."""
        ...

    async def fts_search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> list[ChunkHit]:
        """Top chunks by ts_rank with <mark> headlines; non-deleted notes."""
        ...


def rrf_scores(*rankings: list[str]) -> dict[str, float]:
    """RRF over ranked id lists: score = sum of 1/(k + rank), rank 1-based."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class SearchService:
    def __init__(self, repo: SearchRepo, embedder: EmbedClient):
        self._repo = repo
        self._embedder = embedder

    async def search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> SearchResponse:
        degraded = False
        dense: list[ChunkHit] = []
        try:
            qvec = (await self._embedder.embed([q]))[0]
        except Exception as exc:  # noqa: BLE001 - degraded search, never an error
            degraded = True
            log.warning("search.degraded", error=repr(exc))
        else:
            dense = await self._repo.dense_search(ctx, qvec, domain, LEG_LIMIT)
        fts = await self._repo.fts_search(ctx, q, domain, LEG_LIMIT)
        return SearchResponse(degraded=degraded, results=self._fuse(dense, fts, limit))

    def _fuse(self, dense: list[ChunkHit], fts: list[ChunkHit], limit: int) -> list[SearchResult]:
        scores = rrf_scores([h.chunk_id for h in dense], [h.chunk_id for h in fts])
        dense_ids = {h.chunk_id for h in dense}
        fts_by_id = {h.chunk_id: h for h in fts}
        hits = {h.chunk_id: h for h in [*dense, *fts]}

        # Passage-first but note-grouped: only the best chunk per note shows.
        best_per_note: dict[str, str] = {}
        for chunk_id, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
            note_id = hits[chunk_id].note_id
            best_per_note.setdefault(note_id, chunk_id)

        results = []
        for chunk_id in list(best_per_note.values())[:limit]:
            hit = hits[chunk_id]
            in_fts = chunk_id in fts_by_id
            match: Match = (
                "both" if in_fts and chunk_id in dense_ids else "keyword" if in_fts else "semantic"
            )
            headline = fts_by_id[chunk_id].headline if in_fts else None
            results.append(
                SearchResult(
                    note_id=hit.note_id,
                    chunk_id=chunk_id,
                    snippet=headline or truncate(hit.text, SNIPPET_CHARS),
                    match=match,
                    score=scores[chunk_id],
                    domain=hit.domain,
                    destination=hit.destination,
                    created_at=hit.created_at,
                    body_preview=truncate(hit.body, PREVIEW_CHARS),
                    attachment_count=hit.attachment_count,
                    source_kind=hit.source_kind,
                    source_anchor=hit.source_anchor,
                )
            )
        return results
