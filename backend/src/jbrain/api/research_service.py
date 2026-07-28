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
from jbrain.external import corpus, report_groups, research_corpus, research_shares
from jbrain.storage import BlobStore


class ResearchLibrary:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        embedder: EmbedClient,
        blobs: BlobStore | None = None,
    ) -> None:
        self._maker = maker
        self._embedder = embedder
        # The blob store backs frame-thumbnail redemption for the video detail; None (e.g. a
        # store-less test) degrades every frame to a bare marker instead of failing.
        self._blobs = blobs

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

    # --- report folders (owner-only browse metadata; every call under an owner ctx) ---
    async def list_report_groups(self, ctx: SessionContext) -> list[report_groups.ReportGroup]:
        return await report_groups.list_report_groups(self._maker, ctx)

    async def create_report_group(
        self, ctx: SessionContext, *, name: str
    ) -> report_groups.ReportGroup:
        return await report_groups.create_report_group(self._maker, ctx, name=name)

    async def rename_report_group(
        self, ctx: SessionContext, group_id: str, *, name: str
    ) -> report_groups.ReportGroup | None:
        return await report_groups.rename_report_group(self._maker, ctx, group_id, name=name)

    async def delete_report_group(self, ctx: SessionContext, group_id: str) -> None:
        await report_groups.delete_report_group(self._maker, ctx, group_id)

    async def report_group_exists(self, ctx: SessionContext, group_id: str) -> bool:
        return await report_groups.report_group_exists(self._maker, ctx, group_id)

    async def set_report_group(
        self, ctx: SessionContext, report_id: str, group_id: str | None
    ) -> bool:
        return await report_groups.set_report_group(self._maker, ctx, report_id, group_id)

    # --- report share links (owner mint/list/revoke; public resolve/read) ---
    async def mint_share(
        self,
        ctx: SessionContext,
        *,
        target_kind: str,
        report_id: str | None,
        group_id: str | None,
        label: str | None,
    ) -> tuple[str, research_shares.ShareLink]:
        return await research_shares.mint_share(
            self._maker,
            ctx,
            target_kind=target_kind,
            report_id=report_id,
            group_id=group_id,
            label=label,
        )

    async def list_shares(self, ctx: SessionContext) -> list[research_shares.ShareLink]:
        return await research_shares.list_shares(self._maker, ctx)

    async def revoke_share(self, ctx: SessionContext, share_id: str) -> bool:
        return await research_shares.revoke_share(self._maker, ctx, share_id)

    async def resolve_share(self, token: str) -> research_shares.ResolvedShare | None:
        return await research_shares.resolve_share(self._maker, token)

    async def fetch_shared_report(
        self, link_id: str, report_id: str
    ) -> research_corpus.ReportRecord | None:
        return await research_shares.fetch_shared_report(self._maker, link_id, report_id)

    async def list_group_reports(
        self, link_id: str
    ) -> list[research_corpus.LibraryReport]:
        return await research_shares.list_group_reports(self._maker, link_id)

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

    async def resolve_frames(self, frames: list[dict]) -> list[dict]:
        """The video detail's stored frames as card-ready views — each `thumb_id` redeemed into
        an inline `thumb_data_uri` from its blob so the filmstrip renders stills, not markers."""
        return await corpus.resolve_frame_thumbnails(frames, self._blobs)

    async def delete_video(self, ctx: SessionContext, source_id: str) -> bool:
        return await corpus.delete_external_video(self._maker, ctx, source_id)
