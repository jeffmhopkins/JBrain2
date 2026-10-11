"""Map share links: mint / list / revoke (owner) + resolve (public).

The single writer + reader for `app.mc_share_links` (0230). A link pins one world slot (and
its folder, which the travel log is keyed by) and the player the map opens on; it lives
until revoked. The owner mints it (secret shown once, only its SHA-256 stored), lists and
revokes it under the owner context. A visitor's token resolves under the empty-pin share
context, then every travel-log read re-opens pinned to the link id
(`mc_share_context(link_id)`) so the `*_share` policies admit that world and nothing else.
Mirrors `external/jlaunch_shares.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth import keys
from jbrain.db.session import SessionContext, mc_share_context, scoped_session
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()

_FIELDS = "id, slot, world, xuid, gamertag, created_at, revoked_at, last_viewed_at, view_count"


@dataclass(frozen=True)
class ShareLink:
    """One map share link: what it opens on and its usage, never the secret."""

    id: str
    slot: str
    world: str
    xuid: str
    gamertag: str
    created_at: datetime
    revoked_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int


def _row(row: Any) -> ShareLink:
    return ShareLink(
        id=str(row.id),
        slot=row.slot,
        world=row.world,
        xuid=row.xuid,
        gamertag=row.gamertag,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_viewed_at=row.last_viewed_at,
        view_count=int(row.view_count or 0),
    )


async def mint_share(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    slot: str,
    world: str,
    xuid: str,
    gamertag: str,
) -> tuple[str, ShareLink]:
    """Mint a link under the OWNER's context; the bearer secret is returned exactly once."""
    secret = keys.generate_capability_key()
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.mc_share_links"
                    " (token_hash, slot, world, xuid, gamertag, principal_id)"
                    " VALUES (:h, :slot, :world, :xuid, :tag, cast(:pid AS uuid))"
                    f" RETURNING {_FIELDS}"
                ),
                {
                    "h": keys.hash_token(secret),
                    "slot": slot,
                    "world": world,
                    "xuid": xuid,
                    "tag": gamertag,
                    "pid": ctx.principal_id,
                },
            )
        ).one()
    link = _row(row)
    log.info("mc_share_mint", share_id=link.id, slot=slot)
    return secret, link


async def list_shares(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> list[ShareLink]:
    """The owner's live (unrevoked) links, newest first."""
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_FIELDS} FROM app.mc_share_links"
                    " WHERE revoked_at IS NULL ORDER BY created_at DESC, id"
                )
            )
        ).all()
    return [_row(r) for r in rows]


async def revoke_share(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, share_id: str
) -> bool:
    """Revoke a link (owner). True when a live link was revoked; a non-uuid, unknown or
    already-revoked id is a clean False. The visitor's next read fails closed."""
    try:
        uuid.UUID(share_id)
    except ValueError:
        return False
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.mc_share_links SET revoked_at = now()"
                    " WHERE id = cast(:id AS uuid) AND revoked_at IS NULL RETURNING id"
                ),
                {"id": share_id},
            )
        ).first()
    if row is not None:
        log.info("mc_share_revoke", share_id=share_id)
    return row is not None


async def resolve_share(
    maker: async_sessionmaker[AsyncSession], token: str, *, count_view: bool = False
) -> ShareLink | None:
    """A bearer token's live link, or None (empty/unknown/revoked). `count_view` bumps the
    open counter, under the system context since the visitor can't write: only the page
    open counts, not each of the many tile and poll reads behind it."""
    token = (token or "").strip()
    if not token:
        return None
    async with scoped_session(maker, mc_share_context()) as session:
        row = (
            await session.execute(
                text(
                    f"SELECT {_FIELDS} FROM app.mc_share_links"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"h": keys.hash_token(token)},
            )
        ).first()
    if row is None:
        return None
    link = _row(row)
    if count_view:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            await session.execute(
                text(
                    "UPDATE app.mc_share_links"
                    " SET view_count = view_count + 1, last_viewed_at = now() WHERE id = :id"
                ),
                {"id": link.id},
            )
    return link
