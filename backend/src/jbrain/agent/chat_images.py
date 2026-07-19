"""Shared persistence + view for chat images that aren't generations — a still
grabbed from a video (`grab_frame`) or an image fetched from a URL (`fetch_image`),
per docs/plans/VIDEO_IMAGE_TOOLS_PLAN.md.

Both land in `app.generated_images` (owner-only, migration 0077) with a `provenance`
stamp (migration 0139) so `analyze_image`/`compare_images` resolve them by id, while
the gallery hides them (a fetched product photo is not a render the owner made). The
blob goes through `BlobStore` (rule 2); the row is written on the caller's RLS-scoped
session (rule 3). The view mirrors the `generated_image` component's data slots so a
grabbed/fetched still renders through the existing card (with its provenance so the
card labels the origin, not "seed 0 · web_fetch").
"""

from __future__ import annotations

import io

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import ViewPayload
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.images import GeneratedImage, GeneratedImageRepo
from jbrain.storage import BlobStore

# The provenance stamps (migration 0139): a frame grabbed from a video, an image
# fetched from a URL. NULL provenance stays a real generation/edit.
PROVENANCE_FRAME = "ffmpeg"
PROVENANCE_FETCHED = "web_fetch"

# Bound the DECODED image so a decompression bomb can't OOM the worker — the encoded
# byte cap (e.g. fetch_image's 2 MB) does not bound this: a flat ~2 MB PNG decodes to
# gigapixels. Checked from the header dimensions, before any full pixel decode.
MAX_IMAGE_PIXELS = 40_000_000  # ~40 MP — far above any real frame/photo, well under OOM


class UndecodableImage(ValueError):
    """The bytes are not a decodable image (a fetched HTML error page, a truncated
    grab). Surfaced to the model as a clean tool error, never a stored non-image."""


class ImageTooLarge(ValueError):
    """The image's pixel count exceeds MAX_IMAGE_PIXELS — refused before decode so a
    decompression bomb can't exhaust memory."""


def sniff_image_media_type(data: bytes) -> str | None:
    """The image media type implied by the leading magic bytes, or None when the bytes
    are not one of the web image formats. A STRICT allowlist (reject-on-None) — unlike a
    lenient sniff that defaults to png, this refuses an HTML error page or a hostile
    payload before it is stored or handed to the vision model as "an image"."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(data: bytes) -> tuple[int, int]:
    """(width, height) read from the image HEADER — no full decode, so a decompression
    bomb is rejected on its declared size, not by allocating it. Raises UndecodableImage
    for non-image bytes and ImageTooLarge past the pixel cap."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = int(img.width), int(img.height)
    except Exception as exc:  # noqa: BLE001 - any PIL failure means "not a usable image"
        raise UndecodableImage("that wasn't a readable image") from exc
    if width <= 0 or height <= 0:
        raise UndecodableImage("that image had no dimensions")
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageTooLarge("that image is too large to handle")
    return width, height


async def persist_chat_image(
    maker: async_sessionmaker[AsyncSession],
    session_ctx: SessionContext,
    blobs: BlobStore,
    repo: GeneratedImageRepo,
    *,
    data: bytes,
    provenance: str,
    model: str,
    prompt: str,
) -> GeneratedImage:
    """Store `data` as a first-class chat image: validate + size it (raising on a
    non-image / oversized input), put the blob (rule 2), and insert one owner-only
    `generated_images` row stamped with `provenance` on the caller's RLS-scoped
    session (rule 3). `steps`/`seed` are 0 (not meaningful for a non-generation);
    `kind` stays 'generate' (the behaviour column) — `provenance` carries the origin."""
    width, height = image_dimensions(data)  # raises UndecodableImage / ImageTooLarge
    sha = await blobs.put(data)
    async with scoped_session(maker, session_ctx) as session:
        return await repo.insert(
            session,
            blob_sha256=sha,
            kind="generate",
            model=model,
            prompt=prompt,
            source_sha256=None,
            width=width,
            height=height,
            steps=0,
            seed=0,
            provenance=provenance,
        )


def chat_image_view(row: GeneratedImage) -> ViewPayload:
    """The `generated_image` card for a grabbed/fetched still — the same data slots the
    gen tools' `generated_image_view` builds, so the still renders through the existing
    component. Carries `provenance` so the card labels the origin ("grabbed from
    video"/"fetched from web") and drops the seed/model line. No URL (invariant #9): the
    component builds the `<img>` src from `image_id`."""
    return ViewPayload(
        view="generated_image",
        surface="inline",
        data={
            "image_id": str(row.id),
            "kind": row.kind,
            "prompt": row.prompt,
            "width": row.width,
            "height": row.height,
            "model": row.model,
            "seed": row.seed,
            "provenance": row.provenance,
        },
    )
