"""The `ResearchLibrary` reader — a thin, injectable seam over the two corpus modules.

It holds the session maker + embed client and forwards to the existing
`external.research_corpus` / `external.corpus` callables (list / search / fetch / delete),
so the API router depends on one collaborator on `app.state` (the `RunLogReader` precedent)
and a unit test can inject a fake without monkeypatching module internals. It adds no
behaviour of its own — the RLS scoping lives in the corpus functions (reads build the
`external` scope from `principal_id`; deletes run under the caller-supplied owner context).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext
from jbrain.embed import EmbedClient
from jbrain.external import corpus, research_corpus


class ResearchLibrary:
    def __init__(self, maker: async_sessionmaker[AsyncSession], embedder: EmbedClient) -> None:
        self._maker = maker
        self._embedder = embedder

    # --- reports ---
    async def list_reports(
        self, principal_id: str, *, limit: int, offset: int
    ) -> tuple[list[research_corpus.LibraryReport], int]:
        return await research_corpus.list_reports(
            self._maker, limit=limit, offset=offset, principal_id=principal_id
        )

    async def search_reports(
        self, principal_id: str, query: str, limit: int
    ) -> tuple[list[research_corpus.ReportHit], bool]:
        return await research_corpus.search_reports(
            self._maker, self._embedder, query, limit, principal_id=principal_id
        )

    async def fetch_report(
        self, principal_id: str, ref: str
    ) -> research_corpus.ReportRecord | None:
        return await research_corpus.fetch_report(self._maker, ref, principal_id=principal_id)

    async def delete_report(self, ctx: SessionContext, report_id: str) -> bool:
        return await research_corpus.delete_report(self._maker, ctx, report_id)

    # --- videos ---
    async def list_videos(
        self, principal_id: str, *, limit: int, offset: int
    ) -> tuple[list[corpus.LibraryVideo], int]:
        return await corpus.list_corpus(
            self._maker, limit=limit, offset=offset, principal_id=principal_id
        )

    async def search_videos(
        self, principal_id: str, query: str, limit: int
    ) -> tuple[list[corpus.CorpusHit], bool]:
        return await corpus.search_corpus(
            self._maker, self._embedder, query, limit, principal_id=principal_id
        )

    async def fetch_video(
        self, principal_id: str, video_id: str
    ) -> corpus.ExternalTranscript | None:
        return await corpus.fetch_transcript(self._maker, video_id, principal_id=principal_id)

    async def delete_video(self, ctx: SessionContext, source_id: str) -> bool:
        return await corpus.delete_external_video(self._maker, ctx, source_id)
