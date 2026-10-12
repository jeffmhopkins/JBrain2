"""Public share links for agent chats: mint / list / revoke (owner) + resolve and read (public).

The single writer + reader for `app.chat_share_links` (0238). A link targets ONE chat; the owner
mints it (secret shown once, only its SHA-256 stored), lists and revokes it under the owner
context. A visitor's token resolves under the empty-pin share context, then every read re-opens
PINNED to the link id (`chat_share_context(link_id)`) so the `*_share` policies admit exactly
that chat's session row, turns and runs. The transcript is read through the same
`AgentTranscript.load` the owner's reopen uses, so the visitor gets the same turn shape; its
attachment lookup runs under the visitor context too, where the domain firewall returns none.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.transcript_store import AgentTranscript, TurnRecord
from jbrain.auth import keys
from jbrain.db.session import SessionContext, chat_share_context, scoped_session
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()


@dataclass(frozen=True)
class ChatShareLink:
    """One chat share link in the owner's sheet: its usage, never the secret."""

    id: str
    session_id: str
    created_at: datetime
    last_viewed_at: datetime | None
    view_count: int


@dataclass(frozen=True)
class SharedChat:
    """What a visitor reads: the chat's title and agent, and every turn as of this load."""

    title: str
    agent: str
    last_active_at: datetime
    turns: list[TurnRecord]


_LINK_FIELDS = "id, session_id, created_at, last_viewed_at, view_count"


def _row_to_link(row: Any) -> ChatShareLink:
    return ChatShareLink(
        id=str(row.id),
        session_id=str(row.session_id),
        created_at=row.created_at,
        last_viewed_at=row.last_viewed_at,
        view_count=int(row.view_count or 0),
    )


def _uuid_or_none(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


async def mint_share(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, session_id: str
) -> tuple[str, ChatShareLink] | None:
    """Mint a link to one chat under the OWNER's context; the bearer secret is returned exactly
    once. None when the chat doesn't exist (a non-uuid id included), so a link can't point at
    nothing."""
    sid = _uuid_or_none(session_id)
    if sid is None:
        return None
    secret = keys.generate_capability_key()
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.chat_share_links (token_hash, session_id, principal_id)"
                    " SELECT :h, s.id, cast(:pid AS uuid) FROM app.agent_sessions s"
                    " WHERE s.id = cast(:sid AS uuid)"
                    f" RETURNING {_LINK_FIELDS}"
                ),
                {"h": keys.hash_token(secret), "sid": sid, "pid": ctx.principal_id},
            )
        ).first()
    if row is None:
        return None
    link = _row_to_link(row)
    log.info("chat_share_mint", share_id=link.id, session_id=sid)
    return secret, link


async def list_shares(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, session_id: str
) -> list[ChatShareLink]:
    """One chat's live (non-revoked) links, newest first — usage only, no secrets."""
    sid = _uuid_or_none(session_id)
    if sid is None:
        return []
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_LINK_FIELDS} FROM app.chat_share_links"
                    " WHERE session_id = cast(:sid AS uuid) AND revoked_at IS NULL"
                    " ORDER BY created_at DESC, id"
                ),
                {"sid": sid},
            )
        ).all()
    return [_row_to_link(r) for r in rows]


async def revoke_share(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, share_id: str
) -> bool:
    """Revoke a link (owner). True when a live link was revoked; a non-uuid / unknown /
    already-revoked id is a clean False. The public read fails closed at once: every policy
    re-checks `revoked_at IS NULL` on each visit."""
    lid = _uuid_or_none(share_id)
    if lid is None:
        return False
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.chat_share_links SET revoked_at = now()"
                    " WHERE id = cast(:id AS uuid) AND revoked_at IS NULL RETURNING id"
                ),
                {"id": lid},
            )
        ).first()
    if row is not None:
        log.info("chat_share_revoke", share_id=lid)
    return row is not None


async def resolve_share(
    maker: async_sessionmaker[AsyncSession], token: str, *, count_view: bool = False
) -> tuple[str, str] | None:
    """A bearer token's live link as `(link_id, session_id)`, or None (empty/unknown/revoked).
    Reads by hash under the no-pin share context; `count_view` bumps the counter best-effort
    under the SYSTEM context, since the visitor has no write anywhere."""
    token = (token or "").strip()
    if not token:
        return None
    async with scoped_session(maker, chat_share_context()) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, session_id FROM app.chat_share_links"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"h": keys.hash_token(token)},
            )
        ).first()
    if row is None:
        return None
    if count_view:
        async with scoped_session(maker, SYSTEM_CTX) as session:
            await session.execute(
                text(
                    "UPDATE app.chat_share_links"
                    " SET view_count = view_count + 1, last_viewed_at = now() WHERE id = :id"
                ),
                {"id": str(row.id)},
            )
    return str(row.id), str(row.session_id)


async def read_shared_chat(
    maker: async_sessionmaker[AsyncSession],
    transcript: AgentTranscript,
    link_id: str,
    session_id: str,
) -> SharedChat | None:
    """The chat a link grants, read PINNED to `link_id`: its header and every turn as of now.
    None when RLS admits no session row (the link was revoked between resolve and read)."""
    ctx = chat_share_context(link_id)
    async with scoped_session(maker, ctx) as session:
        head = (
            await session.execute(
                text(
                    "SELECT title, agent, last_active_at FROM app.agent_sessions"
                    " WHERE id = cast(:sid AS uuid)"
                ),
                {"sid": session_id},
            )
        ).first()
    if head is None:
        return None
    turns = await transcript.load(ctx, session_id)
    return SharedChat(
        title=head.title, agent=head.agent, last_active_at=head.last_active_at, turns=turns
    )


async def shared_hosts(maker: async_sessionmaker[AsyncSession], link_id: str) -> set[str]:
    """Every web host the shared chat's tool steps reached, read pinned to `link_id` — the
    allowlist for the share's favicon route, so a token can't make the box fetch arbitrary
    sites' icons."""
    async with scoped_session(maker, chat_share_context(link_id)) as session:
        rows = (
            await session.execute(
                text(
                    # A malformed step must read as no hosts, not an error: each array is
                    # guarded before it is expanded.
                    "SELECT DISTINCT ws->>'url' AS url FROM app.agent_turns t,"
                    " jsonb_array_elements(CASE WHEN jsonb_typeof(t.tools) = 'array'"
                    "   THEN t.tools ELSE '[]'::jsonb END) AS step,"
                    " jsonb_array_elements(CASE WHEN jsonb_typeof(step->'web_sources') = 'array'"
                    "   THEN step->'web_sources' ELSE '[]'::jsonb END) AS ws"
                )
            )
        ).all()
    hosts: set[str] = set()
    for row in rows:
        host = urlsplit(str(row.url or "")).hostname
        if host:
            hosts.add(host.lower())
    return hosts
