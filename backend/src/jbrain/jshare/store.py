"""The single reader and writer of `app.jshare_items` (migration 0252).

The owner adds, lists, renames and deletes items under their own context; a visitor reads
one item under `jshare_context(slug)`, where RLS admits that slug's row and nothing else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.blob_refs import blob_referenced
from jbrain.db.session import SessionContext, jshare_context, scoped_session
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()

_FIELDS = (
    "id, slug, title, kind, filename, media_type, size_bytes, blob_sha256, site_root,"
    " created_at, last_viewed_at, view_count"
)


@dataclass(frozen=True)
class ShareItem:
    id: str
    slug: str
    title: str
    kind: str
    filename: str
    media_type: str
    size_bytes: int
    blob_sha256: str
    site_root: str
    created_at: datetime
    last_viewed_at: datetime | None
    view_count: int


def _row(row: Any) -> ShareItem:
    return ShareItem(
        id=str(row.id),
        slug=row.slug,
        title=row.title,
        kind=row.kind,
        filename=row.filename,
        media_type=row.media_type,
        size_bytes=int(row.size_bytes),
        blob_sha256=row.blob_sha256,
        site_root=row.site_root,
        created_at=row.created_at,
        last_viewed_at=row.last_viewed_at,
        view_count=int(row.view_count or 0),
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def add(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    slug: str,
    title: str,
    kind: str,
    filename: str,
    media_type: str,
    size_bytes: int,
    blob_sha256: str,
    site_root: str,
) -> ShareItem:
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.jshare_items (slug, title, kind, filename, media_type,"
                    " size_bytes, blob_sha256, site_root, principal_id)"
                    " VALUES (:slug, :title, :kind, :filename, :media_type, :size,"
                    " :sha, :root, cast(:pid AS uuid))"
                    f" RETURNING {_FIELDS}"
                ),
                {
                    "slug": slug,
                    "title": title,
                    "kind": kind,
                    "filename": filename,
                    "media_type": media_type,
                    "size": size_bytes,
                    "sha": blob_sha256,
                    "root": site_root,
                    "pid": ctx.principal_id,
                },
            )
        ).one()
    item = _row(row)
    log.info("jshare_add", item_id=item.id, kind=kind, size_bytes=size_bytes)
    return item


async def list_items(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> list[ShareItem]:
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(f"SELECT {_FIELDS} FROM app.jshare_items ORDER BY created_at DESC, id")
            )
        ).all()
    return [_row(r) for r in rows]


async def rename(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, item_id: str, title: str
) -> ShareItem | None:
    if not _is_uuid(item_id):
        return None
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.jshare_items SET title = :title"
                    f" WHERE id = cast(:id AS uuid) RETURNING {_FIELDS}"
                ),
                {"id": item_id, "title": title},
            )
        ).first()
    return _row(row) if row is not None else None


async def delete(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, item_id: str
) -> ShareItem | None:
    """Unpublish an item: the row goes, so its link 404s at once. Returns the deleted row
    so the caller can free its blob."""
    if not _is_uuid(item_id):
        return None
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    f"DELETE FROM app.jshare_items WHERE id = cast(:id AS uuid) RETURNING {_FIELDS}"
                ),
                {"id": item_id},
            )
        ).first()
    if row is None:
        return None
    item = _row(row)
    log.info("jshare_delete", item_id=item.id)
    return item


async def blob_in_use(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, sha256: str
) -> bool:
    """Whether anything on the box, another item included, still holds these bytes. Asked
    after the row is gone, under the owner context, which sees every domain."""
    async with scoped_session(maker, ctx) as session:
        return await blob_referenced(session, sha256)


async def fetch_public(maker: async_sessionmaker[AsyncSession], slug: str) -> ShareItem | None:
    """The item a visitor's slug names, read under the visitor's own context so RLS, not this
    WHERE clause, is what limits it to that one row."""
    async with scoped_session(maker, jshare_context(slug)) as session:
        row = (
            await session.execute(
                text(f"SELECT {_FIELDS} FROM app.jshare_items WHERE slug = :slug"),
                {"slug": slug},
            )
        ).first()
    return _row(row) if row is not None else None


async def count_view(maker: async_sessionmaker[AsyncSession], item_id: str) -> None:
    # The visitor has no write policy, so the counter is bumped as the system.
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "UPDATE app.jshare_items SET view_count = view_count + 1,"
                " last_viewed_at = now() WHERE id = cast(:id AS uuid)"
            ),
            {"id": item_id},
        )
