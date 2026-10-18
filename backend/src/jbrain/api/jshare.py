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
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from jbrain.api.deps import OwnerDep, SettingsDep
from jbrain.db.session import SessionContext
from jbrain.jshare import store
from jbrain.jshare.content import (
    MAX_UPLOAD_BYTES,
    ShareRejected,
    classify,
    clean_filename,
    new_slug,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.config import Settings
    from jbrain.storage import BlobStore

log = structlog.get_logger()

router = APIRouter()

# Room left on the volume after an upload, so publishing one video cannot fill the disk the
# database and every other blob live on.
_FREE_MARGIN_BYTES = 1024**3


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


def _public_base(request: Request, settings: Settings) -> str:
    # The link is for other people, so it names the box's public host when one is set,
    # whichever site the owner happens to be using; otherwise the host they reached.
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _out(item: store.ShareItem, base: str) -> ShareItemOut:
    return ShareItemOut(
        id=item.id,
        slug=item.slug,
        title=item.title,
        kind=item.kind,
        filename=item.filename,
        media_type=item.media_type,
        size_bytes=item.size_bytes,
        url=f"{base}/s/{item.slug}/",
        created_at=item.created_at,
        last_viewed_at=item.last_viewed_at,
        view_count=item.view_count,
    )


@router.get("/jshare/items")
async def list_items(
    owner: OwnerDep, request: Request, settings: SettingsDep
) -> list[ShareItemOut]:
    items = await store.list_items(_maker(request), _ctx(owner.id))
    base = _public_base(request, settings)
    return [_out(i, base) for i in items]


@router.post("/jshare/items", status_code=201)
async def add_item(
    owner: OwnerDep,
    request: Request,
    settings: SettingsDep,
    file: UploadFile,
    title: str = Form(default="", max_length=200),
) -> ShareItemOut:
    filename = clean_filename(file.filename)
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="That file is larger than 4 GB.")
    blobs = _blobs(request)
    needed = (file.size or 0) + _FREE_MARGIN_BYTES
    if blobs.free_bytes() < needed:
        raise HTTPException(status_code=507, detail="The box is too full to keep that file.")
    try:
        # Classify before storing anything: a refused zip must not leave a blob behind.
        kind = await asyncio.to_thread(classify, filename, file.file)
    except ShareRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await file.seek(0)

    size = 0

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal size
        while data := await file.read(1 << 20):
            size += len(data)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="That file is larger than 4 GB.")
            yield data

    sha = await blobs.put_stream(chunks())
    item = await store.add(
        _maker(request),
        _ctx(owner.id),
        slug=new_slug(),
        title=title.strip() or filename,
        kind=kind.kind,
        filename=filename,
        media_type=kind.media_type,
        size_bytes=size,
        blob_sha256=sha,
        site_root=kind.site_root,
    )
    return _out(item, _public_base(request, settings))


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
    return _out(item, _public_base(request, settings))


@router.delete("/jshare/items/{item_id}", status_code=204)
async def delete_item(item_id: str, owner: OwnerDep, request: Request) -> None:
    maker, ctx = _maker(request), _ctx(owner.id)
    item = await store.delete(maker, ctx, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="no such item")
    # The same bytes may be a note attachment or another item; only free a file nobody holds.
    if not await store.blob_in_use(maker, ctx, item.blob_sha256):
        await _blobs(request).delete(item.blob_sha256)
