"""The `fetch_image` agent tool: jerv fetches an image from a URL and persists it as a
first-class chat image (docs/plans/VIDEO_IMAGE_TOOLS_PLAN.md, Wave V3).

`web_fetch` returns a page's readable TEXT — it strips images — so jerv is otherwise
blind to any picture on the web (the gap that made it fabricate an "online photo"
comparison it never saw). `fetch_image` closes that: it fetches the bytes through the
SAME per-hop SSRF guard the text fetcher uses (`WebFetcher.fetch_bytes`), validates they
are really an image (a strict magic-byte allowlist — a redirect stub or HTML error page
is refused, never handed to the vision model as "an image"), bounds the decoded pixels
against a decompression bomb (the shared `persist_chat_image`), and stores it as a
`provenance='web_fetch'` row `analyze_image`/`compare_images` can read by id.

Like `web_search`/`web_fetch` this runs DIRECTLY (the bounded jerv-sandbox exception to
invariant #9): only jerv allowlists it, and jerv holds no owner data to leak into the
request. The fetched URL rides back as a `WebSource` so it is a real citation, not
model-authored prose.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.chat_images import (
    PROVENANCE_FETCHED,
    ImageTooLarge,
    UndecodableImage,
    chat_image_view,
    persist_chat_image,
    sniff_image_media_type,
)
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.models.images import GeneratedImageRepo
from jbrain.storage import BlobStore
from jbrain.web.fetch import WebFetcher, WebFetchError

log = structlog.get_logger()


def build_fetch_image_handlers(
    fetcher: WebFetcher,
    blobs: BlobStore,
    repo: GeneratedImageRepo,
    maker: async_sessionmaker[AsyncSession],
    emit: BrainEmit | None = None,
) -> dict[str, ToolHandler]:
    """`fetch_image`, bound to the shared fetcher + image storage. `emit`, if given, fires
    the wall-display tendril event (like web_fetch) when jerv reaches out."""

    async def fetch_image_tool(arguments: dict, ctx: ToolContext) -> str:
        url = str(arguments.get("url", "")).strip()
        if not url:
            return "fetch_image needs a url."
        show = arguments.get("show", True) is not False
        if emit:
            emit("web_fetch", url)
        try:
            content_type, data = await fetcher.fetch_bytes(url)
        except WebFetchError as exc:
            return str(exc)
        # Strict allowlist: refuse anything that isn't one of the web image formats BEFORE
        # storing it or handing it to the vision model (an HTML error page / redirect stub
        # would otherwise masquerade as an image). persist_chat_image then bounds the
        # decoded pixels (decompression-bomb guard).
        if sniff_image_media_type(data) is None:
            got = content_type or "unknown content"
            return f"That URL didn't return an image (got {got}). Give a direct image URL."
        try:
            row = await persist_chat_image(
                maker,
                ctx.session,
                blobs,
                repo,
                data=data,
                provenance=PROVENANCE_FETCHED,
                model="web_fetch",
                prompt=url,
            )
        except (UndecodableImage, ImageTooLarge) as exc:
            return str(exc)

        image_id = str(row.id)
        summary = (
            f"Fetched that image (image_id {image_id}). Use analyze_image with"
            f" source_image_id {image_id} to look at it, or compare_images to compare it"
            " with another image."
        )
        # The fetched URL is a real citation (favicon chip), not model-authored prose.
        return ToolOutput(
            summary,
            view=chat_image_view(row) if show else None,
            web_sources=(WebSource(url=url, title=url),),
        )

    return {"fetch_image": fetch_image_tool}
