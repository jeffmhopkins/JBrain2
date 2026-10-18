"""Every live share link the box has handed out, in one list, and one way to revoke any of them.

Each feature mints its own links into its own table (chats, research, Minecraft maps, jlaunch
results, jcode sessions, intake), with its own mint sheet; this module only reads across them
and revokes through each feature's own revoke, so a revoke here does exactly what one in the
feature's sheet does. JShare's own items are not here: the owner list already carries them.

No URL comes back for any of these: every one of those features stores only the token's hash,
so a link can be shown once, when it is made, and never again. Re-sharing means a new link.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - a dataclass field type
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from jbrain.agent import chat_shares
from jbrain.db.session import SessionContext, scoped_session
from jbrain.external import jlaunch_shares, research_shares
from jbrain.intake import service as intake_service
from jbrain.minecraft import shares as mc_shares

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.auth.service import AuthRepo
    from jbrain.intake.service import IntakeRepo

log = structlog.get_logger()

#: In the order the JShare screen lists them.
KINDS = ("chat", "research", "minecraft", "jlaunch", "jcode", "intake")


@dataclass(frozen=True)
class SharedLink:
    kind: str
    id: str
    title: str
    created_at: datetime
    expires_at: datetime | None
    last_viewed_at: datetime | None
    #: None where the feature does not count opens (jcode).
    view_count: int | None
    #: A short fact about the link's state the title does not carry, or "".
    detail: str = ""


@dataclass(frozen=True)
class Sources:
    """What the readers and revokers need, from `app.state`."""

    maker: async_sessionmaker[AsyncSession]
    auth_repo: AuthRepo
    intake_repo: IntakeRepo
    #: Revokes a jcode link the way its own route does (sockets, preview slug); see
    #: `api/jcode_share.py:revoke_link`. Passed in so this module needs no FastAPI app.
    revoke_jcode: Callable[[str, str], Awaitable[bool]]


async def _rows(maker: async_sessionmaker[AsyncSession], ctx: SessionContext, sql: str) -> Any:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(sql))).all()


async def _chats(src: Sources, ctx: SessionContext) -> list[SharedLink]:
    rows = await _rows(
        src.maker,
        ctx,
        "SELECT l.id, coalesce(nullif(s.title, ''), 'Untitled chat') AS title, l.created_at,"
        " l.last_viewed_at, l.view_count FROM app.chat_share_links l"
        " LEFT JOIN app.agent_sessions s ON s.id = l.session_id"
        " WHERE l.revoked_at IS NULL",
    )
    return [
        SharedLink("chat", str(r.id), r.title, r.created_at, None, r.last_viewed_at, r.view_count)
        for r in rows
    ]


async def _research(src: Sources, ctx: SessionContext) -> list[SharedLink]:
    rows = await _rows(
        src.maker,
        ctx,
        "SELECT l.id, l.target_kind, coalesce(nullif(l.label, ''), nullif(r.title, ''),"
        " r.question, g.name, 'Research') AS title, l.created_at, l.last_viewed_at,"
        " l.view_count FROM app.research_share_links l"
        " LEFT JOIN app.research_reports r ON r.id = l.report_id"
        " LEFT JOIN app.report_groups g ON g.id = l.group_id"
        " WHERE l.revoked_at IS NULL",
    )
    return [
        SharedLink(
            "research",
            str(r.id),
            r.title,
            r.created_at,
            None,
            r.last_viewed_at,
            r.view_count,
            "folder" if r.target_kind == "group" else "report",
        )
        for r in rows
    ]


async def _minecraft(src: Sources, ctx: SessionContext) -> list[SharedLink]:
    return [
        SharedLink(
            "minecraft",
            link.id,
            f"{link.world} map" if link.world else "Minecraft map",
            link.created_at,
            None,
            link.last_viewed_at,
            link.view_count,
            link.gamertag,
        )
        for link in await mc_shares.list_shares(src.maker, ctx)
    ]


async def _jlaunch(src: Sources, ctx: SessionContext) -> list[SharedLink]:
    rows = await _rows(
        src.maker,
        ctx,
        "SELECT l.id, coalesce(nullif(l.label, ''), nullif(r.title, ''), nullif(r.spec_name, ''),"
        " 'Results') AS title, l.created_at, l.last_viewed_at, l.view_count"
        " FROM app.jlaunch_share_links l LEFT JOIN app.jlaunch_runs r ON r.id = l.run_id"
        " WHERE l.revoked_at IS NULL",
    )
    return [
        SharedLink(
            "jlaunch", str(r.id), r.title, r.created_at, None, r.last_viewed_at, r.view_count
        )
        for r in rows
    ]


async def _jcode(src: Sources, _ctx: SessionContext) -> list[SharedLink]:
    return [
        SharedLink(
            "jcode",
            token.id,
            token.label,
            token.created_at,
            token.expires_at,
            token.last_used_at,
            None,
            "opened" if token.redeemed_at is not None else "not opened yet",
        )
        for token, _sid in await src.auth_repo.list_all_jcode_shares()
    ]


async def _intake(src: Sources, ctx: SessionContext) -> list[SharedLink]:
    out: list[SharedLink] = []
    for link in await src.intake_repo.list_links(ctx):
        # Only links that still let someone in: the owner's intake screen keeps the history.
        if link.status != "active" or link.expires_at <= datetime.now(link.expires_at.tzinfo):
            continue
        out.append(
            SharedLink(
                "intake",
                link.id,
                link.label or link.domain_code,
                link.created_at,
                link.expires_at,
                None,
                link.opens_used,
                f"{link.runs_used} of {link.max_runs} used",
            )
        )
    return out


_READERS: dict[str, Callable[[Sources, SessionContext], Awaitable[list[SharedLink]]]] = {
    "chat": _chats,
    "research": _research,
    "minecraft": _minecraft,
    "jlaunch": _jlaunch,
    "jcode": _jcode,
    "intake": _intake,
}


async def list_links(src: Sources, ctx: SessionContext) -> tuple[list[SharedLink], list[str]]:
    """Every live link, newest first, and the kinds that could not be read. One feature's
    failure must not hide the rest: the point of the list is seeing what is still open."""
    links: list[SharedLink] = []
    failed: list[str] = []
    for kind in KINDS:
        try:
            links.extend(await _READERS[kind](src, ctx))
        except Exception as exc:  # noqa: BLE001 - reported per kind, never raised
            log.warning("jshare_links_read_failed", kind=kind, error=repr(exc))
            failed.append(kind)
    links.sort(key=lambda link: link.created_at, reverse=True)
    return links, failed


async def revoke(src: Sources, ctx: SessionContext, kind: str, link_id: str) -> bool:
    """Revoke one link through its own feature's revoke. False for an unknown kind, or a link
    that is unknown or already revoked."""
    if kind == "chat":
        return await chat_shares.revoke_share(src.maker, ctx, link_id)
    if kind == "research":
        return await research_shares.revoke_share(src.maker, ctx, link_id)
    if kind == "minecraft":
        return await mc_shares.revoke_share(src.maker, ctx, link_id)
    if kind == "jlaunch":
        return await jlaunch_shares.revoke_share(src.maker, ctx, link_id)
    if kind == "jcode":
        try:
            # Canonical, so the socket registry (keyed by principal id) is hit.
            link_id = str(uuid.UUID(link_id))
        except ValueError:
            return False
        sid = await src.auth_repo.jcode_share_session(link_id)
        return sid is not None and await src.revoke_jcode(sid, link_id)
    if kind == "intake":
        return await intake_service.revoke_intake_link(src.intake_repo, ctx, link_id)
    return False
