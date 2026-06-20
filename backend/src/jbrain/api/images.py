"""Profile-image helpers (Phase-6 profile-image chain): size cap + magic-byte sniffing.

Uploaded image bytes are content-addressed in the blob store (sha256) and served back by
`FileResponse`. We never trust the client's `Content-Type`: the media type is derived from the
bytes' magic number both to reject non-images on upload and to label the response on serve.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import OwnerDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.db.session import scoped_session
from jbrain.models.images import GeneratedImage, GeneratedImageRepo
from jbrain.storage import BlobStore

# A profile image is small; cap well below the 100MB attachment limit.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def sniff_image_type(header: bytes) -> str | None:
    """The image media type implied by the leading magic bytes, or None when unrecognised."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_path(path: Path) -> str:
    """Sniff a stored blob's media type from its first bytes (octet-stream fallback)."""
    try:
        with path.open("rb") as fh:
            return sniff_image_type(fh.read(16)) or "application/octet-stream"
    except OSError:
        return "application/octet-stream"


# Generated images are owner-only chat artifacts (Wave G2). The gate is the OwnerDep
# *and* RLS: a non-owner is refused at the dependency, and the owner-only `generated_images`
# policy means a row a query can't see is simply absent — a missing/forbidden row is one
# clean 404, never a 403 that would confirm an id exists.
generated_router = APIRouter(prefix="/images/generated", dependencies=[Depends(owner_only)])


def _get_generated_repo(request: Request) -> GeneratedImageRepo:
    return cast(GeneratedImageRepo, request.app.state.generated_image_repo)


def _get_blob_store(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def _get_session_maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


@generated_router.get("/{image_id}")
async def serve_generated_image(image_id: str, owner: OwnerDep, request: Request) -> FileResponse:
    """Serve a generated image's bytes by id. Owner-gated; the lookup runs on the owner's
    RLS-scoped session, so a row the owner can't see is a 404. The media type is sniffed
    from the bytes — a stored content-type is never trusted."""
    repo = _get_generated_repo(request)
    blobs = _get_blob_store(request)
    async with scoped_session(_get_session_maker(request), ctx_for(owner)) as session:
        row: GeneratedImage | None = await repo.get(session, image_id)
    if row is None or not await blobs.exists(row.blob_sha256):
        raise HTTPException(status_code=404, detail="generated image not found")
    path = blobs.path_for(row.blob_sha256)
    # nosniff: served inline with a magic-byte-derived type, so the browser must not re-sniff
    # a chameleon file (image header + executable tail) into something it would run.
    return FileResponse(
        path, media_type=sniff_path(path), headers={"X-Content-Type-Options": "nosniff"}
    )
