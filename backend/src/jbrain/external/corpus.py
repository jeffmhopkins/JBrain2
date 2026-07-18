"""Persist a completed video analysis into the external-source corpus, and enqueue embedding.

The single writer for `app.external_sources` + `app.external_source_chunks`: the deferred
full-mode `analyze_stream` job calls this after it finishes, reusing the analysis it already
produced (zero extra vision/whisper cost). Keyed on `(provider, video_id)` so a re-analysis
upserts in place and rebuilds the source's passages; a stream with no provider video id (a
bare media URL) is skipped — there is nothing to dedup or deep-link back to.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.external.window import window_timeline
from jbrain.ingest.video import VideoAnalysis
from jbrain.queue import SYSTEM_CTX, enqueue
from jbrain.search.service import rrf_scores
from jbrain.stream import ResolvedStream
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

KIND_EMBED_EXTERNAL_SOURCE = "embed_external_source"

# In-code only (dispatch-only, no Ops trigger, so it stays out of main.py's API_ACTION_SPECS):
# the corpus sibling of embed_note, kicked as a follow-up by persist_analysis. The worker adds
# it to its build_registry tuple; the boot-time registry bijection requires a spec per handler.
EMBED_EXTERNAL_SOURCE_SPEC = ActionSpec(
    name=KIND_EMBED_EXTERNAL_SOURCE,
    version=1,
    handler=KIND_EMBED_EXTERNAL_SOURCE,
    domain_optional=True,
    mutating=True,  # writes chunk + summary embeddings
    cost_class="standard",  # local embed container, no LLM router
    description="Fill NULL embeddings for one external-source video (passage chunks + summary).",
    category="maintenance",
)


def _published_at(upload_date: str) -> datetime | None:
    """yt-dlp's YYYYMMDD upload string → a day-precision datetime, or None if malformed."""
    if len(upload_date) == 8 and upload_date.isdigit():
        try:
            return datetime.strptime(upload_date, "%Y%m%d")
        except ValueError:
            return None
    return None


def _transcript_source_label(source: str, resolved: ResolvedStream) -> str:
    """Refine the pipeline's coarse "captions" into captions:manual / captions:auto from
    the selected track's kind (the pipeline collapses the distinction)."""
    if source == "captions" and resolved.caption is not None:
        return f"captions:{resolved.caption.kind}"
    return source


async def persist_analysis(
    maker: async_sessionmaker[AsyncSession],
    *,
    resolved: ResolvedStream,
    result: VideoAnalysis,
    transcript_source: str,
    origin: str = "adhoc",
) -> str | None:
    """Upsert the corpus row + passages for one analysed video and enqueue embedding.

    Returns the source id, or None when the stream has no provider video id (skipped). The
    upsert refreshes an existing row (a re-analysis) and NULLs its embeddings so the enqueued
    `embed_external_source` re-fills them for the rebuilt summary + passages."""
    if not resolved.video_id:
        return None

    analysis = result.analysis
    windows = window_timeline(analysis)
    frames = analysis.get("frames") or []
    params = {
        "provider": resolved.provider or "youtube",
        "video_id": resolved.video_id,
        "url": resolved.webpage_url,
        "title": resolved.title,
        "channel_id": resolved.channel_id or None,
        "channel_name": resolved.channel_name or None,
        "published_at": _published_at(resolved.upload_date),
        "duration_s": int(resolved.duration_s) if resolved.duration_s else None,
        "duration_ms": analysis.get("duration_ms"),
        "summary": result.summary or None,
        "transcript_source": _transcript_source_label(transcript_source, resolved),
        "frames": json.dumps(frames),
        "tool": result.tool,
        "origin": origin,
    }

    async with scoped_session(maker, SYSTEM_CTX) as session:
        source_id = (
            await session.execute(
                text(
                    "INSERT INTO app.external_sources"
                    " (provider, video_id, url, title, channel_id, channel_name, published_at,"
                    "  duration_s, duration_ms, summary, transcript_source, frames, tool, origin,"
                    "  status, analyzed_at)"
                    " VALUES (:provider, :video_id, :url, :title, :channel_id, :channel_name,"
                    "  :published_at, :duration_s, :duration_ms, :summary, :transcript_source,"
                    "  cast(:frames AS jsonb), :tool, :origin, 'done', now())"
                    " ON CONFLICT (provider, video_id) DO UPDATE SET"
                    "  url = EXCLUDED.url, title = EXCLUDED.title,"
                    "  channel_id = EXCLUDED.channel_id, channel_name = EXCLUDED.channel_name,"
                    "  published_at = EXCLUDED.published_at, duration_s = EXCLUDED.duration_s,"
                    "  duration_ms = EXCLUDED.duration_ms, summary = EXCLUDED.summary,"
                    "  transcript_source = EXCLUDED.transcript_source, frames = EXCLUDED.frames,"
                    "  tool = EXCLUDED.tool, status = 'done', last_error = NULL,"
                    "  analyzed_at = now(),"
                    "  summary_embedding = NULL, embedding_model = NULL"
                    " RETURNING id"
                ),
                params,
            )
        ).scalar_one()
        source_id = str(source_id)
        # Rebuild passages wholesale (delete + re-insert), so a re-analysis has no stale
        # rows; chunk embeddings start NULL and are filled by embed_external_source.
        await session.execute(
            text("DELETE FROM app.external_source_chunks WHERE source_id = :sid"),
            {"sid": source_id},
        )
        if windows:
            await session.execute(
                text(
                    "INSERT INTO app.external_source_chunks (source_id, seq, t_ms, text)"
                    " VALUES (:sid, :seq, :t_ms, :text)"
                ),
                [
                    {"sid": source_id, "seq": w.seq, "t_ms": w.t_ms, "text": w.text}
                    for w in windows
                ],
            )

    await enqueue(maker, SYSTEM_CTX, KIND_EMBED_EXTERNAL_SOURCE, {"source_id": source_id})
    log.info(
        "external.persisted",
        source_id=source_id,
        video_id=resolved.video_id,
        passages=len(windows),
        origin=origin,
    )
    return source_id


# --- corpus search (the search_external tool's engine) --------------------------------

# One hybrid RRF query over the corpus, mirroring SearchService: a dense + FTS leg over the
# passage chunks and a source-level summary-dense leg, fused per-source (best_per_source), so
# one video surfaces once with its best-matching passage. LEG_LIMIT ~ SearchService's.
_LEG_LIMIT = 40

_CHUNK_COLS = (
    "SELECT c.source_id, c.t_ms, c.text AS passage, s.title, s.channel_name, s.url"
    " FROM app.external_source_chunks c JOIN app.external_sources s ON s.id = c.source_id"
)
_CHUNK_DENSE_SQL = (
    _CHUNK_COLS + " WHERE c.embedding IS NOT NULL"
    " ORDER BY c.embedding <=> cast(:qvec AS vector), c.id LIMIT :limit"
)
_CHUNK_FTS_SQL = (
    _CHUNK_COLS + " WHERE c.tsv @@ websearch_to_tsquery('english', :q)"
    " ORDER BY ts_rank(c.tsv, websearch_to_tsquery('english', :q)) DESC, c.id LIMIT :limit"
)
_SUMMARY_DENSE_SQL = (
    "SELECT s.id AS source_id, s.title, s.channel_name, s.url, s.summary AS passage"
    " FROM app.external_sources s WHERE s.summary_embedding IS NOT NULL"
    " ORDER BY s.summary_embedding <=> cast(:qvec AS vector), s.id LIMIT :limit"
)


@dataclass(frozen=True)
class CorpusHit:
    """One video in a search result: its best-matching passage and (for a chunk hit) the
    real ms offset so the tool can deep-link to the moment."""

    source_id: str
    title: str
    channel_name: str
    url: str
    passage: str
    t_ms: int | None


def _corpus_read_context(principal_id: str) -> SessionContext:
    """The purpose-built read scope for the corpus tools: an owner session restricted to the
    `general` domain ONLY (like a narrowed job context), so a persona whose own session is
    empty-scoped (jerv) can read the general-domain corpus WITHOUT its firewall being widened —
    the handler only ever runs corpus queries under it, so no other owner data is reachable."""
    return SessionContext(
        principal_id=principal_id,
        principal_kind="owner",
        domain_scopes=("general",),
        owner_scoped=True,
    )


async def search_corpus(
    maker: async_sessionmaker[AsyncSession],
    embedder: EmbedClient,
    query: str,
    limit: int,
    *,
    principal_id: str = "",
) -> tuple[list[CorpusHit], bool]:
    """Hybrid RRF search over the corpus. Returns (hits, degraded); `degraded` is True when the
    embed container was unreachable and only the keyword leg ran (mirrors SearchService)."""
    try:
        qvec: list[float] | None = (await embedder.embed([query]))[0]
    except Exception:  # noqa: BLE001 - embed container down → keyword-only, like SearchService
        qvec, degraded = None, True
    else:
        degraded = False

    ctx = _corpus_read_context(principal_id)
    # A chunk row per source for display (first seen across the legs = its best passage), and
    # the per-leg source-id rankings RRF fuses.
    display: dict[str, CorpusHit] = {}
    rankings: list[list[str]] = []

    def ingest_rows(rows: Sequence[Any], *, is_chunk: bool) -> list[str]:
        ranking: list[str] = []
        for r in rows:
            sid = str(r.source_id)
            if sid not in display:
                display[sid] = CorpusHit(
                    source_id=sid,
                    title=r.title or "",
                    channel_name=r.channel_name or "",
                    url=r.url,
                    passage=(r.passage or "").strip(),
                    t_ms=int(r.t_ms) if is_chunk else None,
                )
            if sid not in ranking:
                ranking.append(sid)
        return ranking

    async with scoped_session(maker, ctx) as session:
        fts = (
            await session.execute(text(_CHUNK_FTS_SQL), {"q": query, "limit": _LEG_LIMIT})
        ).all()
        rankings.append(ingest_rows(fts, is_chunk=True))
        if qvec is not None:
            vec = vector_literal(qvec)
            dense = (
                await session.execute(
                    text(_CHUNK_DENSE_SQL), {"qvec": vec, "limit": _LEG_LIMIT}
                )
            ).all()
            rankings.append(ingest_rows(dense, is_chunk=True))
            summ = (
                await session.execute(
                    text(_SUMMARY_DENSE_SQL), {"qvec": vec, "limit": _LEG_LIMIT}
                )
            ).all()
            # Summary hits are a coarse fallback: only add a source the passage legs missed
            # (never overwrite a chunk hit's precise t_ms + passage).
            rankings.append(ingest_rows(summ, is_chunk=False))

    scores = rrf_scores(*rankings)
    ranked = sorted(scores, key=lambda s: (scores[s], s), reverse=True)[:limit]
    return [display[sid] for sid in ranked], degraded
