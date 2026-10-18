"""The owner's side of JShare: publish a file at its own link, list, rename, unpublish.

Every route is owner-only. The public side, which serves what is published here, is
`jshare_public.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime symbol
from typing import TYPE_CHECKING, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep, SettingsDep
from jbrain.db.session import SessionContext
from jbrain.jshare import store
from jbrain.jshare.content import (
    MAX_UPLOAD_BYTES,
    Classified,
    ShareRejected,
    classify,
    clean_filename,
    new_slug,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.config import Settings
    from jbrain.storage import BlobStore

log = structlog.get_logger()

router = APIRouter()

# Room left on the volume after an upload, so publishing one video cannot fill the disk the
# database and every other blob live on.
_FREE_MARGIN_BYTES = 1024**3
_TOO_BIG = "That file is larger than 4 GB."


class ShareItemOut(BaseModel):
    id: str
    slug: str
    title: str
    kind: str
    filename: str
    media_type: str
    size_bytes: int
    url: str
    created_at: datetime
    last_viewed_at: datetime | None
    view_count: int


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


def _blobs(request: Request) -> BlobStore:
    return cast("BlobStore", request.app.state.blob_store)


def _ctx(principal_id: str) -> SessionContext:
    return SessionContext(principal_id=principal_id, principal_kind="owner")


def _public_base(settings: Settings) -> str:
    # The link is for other people, so it names the box's public host whichever site the
    # owner happens to be using (a LAN or owner-site name means nothing to a recipient, and
    # those sites don't serve /s/ at all). Without either setting the PWA names its own origin.
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    if settings.public_domain:
        return f"https://{settings.public_domain}"
    return ""


def _out(item: store.ShareItem, base: str) -> ShareItemOut:
    return ShareItemOut(
        id=item.id,
        slug=item.slug,
        title=item.title,
        kind=item.kind,
        filename=item.filename,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        url=f"{base}/s/{item.slug}/" if base else "",
        created_at=item.created_at,
        last_viewed_at=item.last_viewed_at,
        view_count=item.view_count,
    )


@router.get("/jshare/items")
async def list_items(
    owner: OwnerDep, request: Request, settings: SettingsDep
) -> list[ShareItemOut]:
    items = await store.list_items(_maker(request), _ctx(owner.id))
    base = _public_base(settings)
    return [_out(i, base) for i in items]


@router.post("/jshare/items", status_code=201)
async def add_item(
    owner: OwnerDep,
    request: Request,
    settings: SettingsDep,
    filename: str = Query(min_length=1, max_length=400),
    title: str = Query(default="", max_length=200),
) -> ShareItemOut:
    """The PWA sends the file as the raw request body, so it streams straight into the blob
    store: a multipart form would be spooled whole onto the container's own disk first, where
    no size or free-space check could stop it."""
    name = clean_filename(filename)
    raw_length = request.headers.get("content-length", "")
    if not raw_length.isdigit():
        raise HTTPException(status_code=411, detail="The upload needs a Content-Length.")
    length = int(raw_length)
    if length > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_BIG)
    blobs = _blobs(request)
    if blobs.free_bytes() < length + _FREE_MARGIN_BYTES:
        raise HTTPException(status_code=507, detail="The box is too full to keep that file.")

    size = 0

    async def chunks() -> AsyncIterator[bytes]:
        # Content-Length is the client's word; the bytes that actually arrive are the limit.
        nonlocal size
        async for data in request.stream():
            size += len(data)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=_TOO_BIG)
            yield data

    sha = await blobs.put_stream(chunks())
    maker, ctx = _maker(request), _ctx(owner.id)
    # The bytes are stored before they can be judged, so anything that stops them becoming a
    # row frees them (unless something else on the box holds the same bytes).
    try:
        kind = await asyncio.to_thread(_classify_blob, name, blobs.path_for(sha))
        item = await store.add(
            maker,
            ctx,
            slug=new_slug(),
            title=title.strip() or name,
            kind=kind.kind,
            filename=name,
            media_type=kind.media_type,
            size_bytes=size,
            blob_sha256=sha,
            site_root=kind.site_root,
        )
    except BaseException as exc:
        if not await store.blob_in_use(maker, ctx, sha):
            await blobs.delete(sha)
        if isinstance(exc, ShareRejected):
            raise HTTPException(status_code=422, detail=str(exc)) from None
        raise
    return _out(item, _public_base(settings))


def _classify_blob(name: str, path: Path) -> Classified:
    with path.open("rb") as fh:
        return classify(name, fh)


@router.patch("/jshare/items/{item_id}")
async def rename_item(
    item_id: str, body: RenameRequest, owner: OwnerDep, request: Request, settings: SettingsDep
) -> ShareItemOut:
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="A title cannot be blank.")
    item = await store.rename(_maker(request), _ctx(owner.id), item_id, title)
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    return _out(item, _public_base(settings))


@router.delete("/jshare/items/{item_id}", status_code=204)
async def delete_item(item_id: str, owner: OwnerDep, request: Request) -> None:
    maker, ctx = _maker(request), _ctx(owner.id)
    item = await store.delete(maker, ctx, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    # The same bytes may be a note attachment or another item; only free a file nobody holds.
    if not await store.blob_in_use(maker, ctx, item.blob_sha256):
        await _blobs(request).delete(item.blob_sha256)
