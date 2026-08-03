"""Public share links for jlaunch runs: mint / list / revoke (owner) + resolve (public).

The single writer + reader for `app.jlaunch_share_links` (0153). A link targets ONE run;
the owner mints it (secret shown once, only its SHA-256 hash stored), lists and revokes it
under the owner context, and a public visitor resolves the token to read the run's results.
The read itself is enforced in RLS: `resolve_share` looks the link up by hash under the
empty-scope share context, then the API re-opens PINNED to the link id
(`jlaunch_share_context(link_id)`) so `jlaunch_runs_share` (0153) admits exactly that link's
run and nothing else. Mirrors `external/research_shares.py`.
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
from jbrain.db.session import SessionContext, jlaunch_share_context, scoped_session
from jbrain.models.jlaunch import JlaunchRunRepo, JlaunchRunRow
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()

_LINK_FIELDS = "id, label, run_id, created_at, revoked_at, last_viewed_at, view_count"


@dataclass(frozen=True)
class ShareLink:
    """One share link in the owner's management list — its target + usage, no secret."""

    id: str
    label: str | None
    run_id: str
    created_at: datetime
    revoked_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int


def _row_to_link(row: Any) -> ShareLink:
    return ShareLink(
        id=str(row.id),
        label=row.label,
        run_id=row.run_id,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_viewed_at=row.last_viewed_at,
        view_count=int(row.view_count or 0),
    )


async def mint_share(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    run_id: str,
    label: str | None,
) -> tuple[str, ShareLink]:
    """Mint a link for a run under the OWNER's context; returns the bearer secret exactly
    once alongside its record. Only the secret's SHA-256 is stored. The caller ensures the
    run row exists (the FK requires it) and is a finished, artifact-bearing run."""
    secret = keys.generate_capability_key()
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.jlaunch_share_links"
                    " (token_hash, label, run_id, principal_id)"
                    " VALUES (:h, :label, :rid, cast(:pid AS uuid))"
                    f" RETURNING {_LINK_FIELDS}"
                ),
                {
                    "h": keys.hash_token(secret),
                    "label": label,
                    "rid": run_id,
                    "pid": ctx.principal_id,
                },
            )
        ).one()
    link = _row_to_link(row)
    log.info("jlaunch_share_mint", share_id=link.id, run_id=run_id)
    return secret, link


async def list_shares(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, run_id: str
) -> list[ShareLink]:
    """The owner's live (non-revoked) share links for a run, newest first — no secrets."""
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_LINK_FIELDS} FROM app.jlaunch_share_links"
                    " WHERE run_id = :rid AND revoked_at IS NULL"
                    " ORDER BY created_at DESC, id"
                ),
                {"rid": run_id},
            )
        ).all()
    return [_row_to_link(r) for r in rows]


async def revoke_share(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, share_id: str
) -> bool:
    """Revoke a link (owner). True when a live link was revoked; a non-uuid / unknown /
    already-revoked id is a clean False. Fails the public read closed at once."""
    try:
        uuid.UUID(share_id)
    except ValueError:
        return False
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.jlaunch_share_links SET revoked_at = now()"
                    " WHERE id = cast(:id AS uuid) AND revoked_at IS NULL RETURNING id"
                ),
                {"id": share_id},
            )
        ).first()
    if row is not None:
        log.info("jlaunch_share_revoke", share_id=share_id)
    return row is not None


async def resolve_share(maker: async_sessionmaker[AsyncSession], token: str) -> str | None:
    """Resolve a bearer token to its live link id, or None (empty/unknown/revoked). Reads by
    hash under the no-pin share context, then bumps the view counter best-effort under the
    system context (the visitor has no write policy)."""
    token = (token or "").strip()
    if not token:
        return None
    async with scoped_session(maker, jlaunch_share_context()) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM app.jlaunch_share_links"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"h": keys.hash_token(token)},
            )
        ).first()
    if row is None:
        return None
    link_id = str(row.id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "UPDATE app.jlaunch_share_links"
                " SET view_count = view_count + 1, last_viewed_at = now() WHERE id = :id"
            ),
            {"id": link_id},
        )
    return link_id


async def fetch_shared_run(
    maker: async_sessionmaker[AsyncSession], link_id: str
) -> JlaunchRunRow | None:
    """The run a link exposes, read PINNED to `link_id` so RLS admits only that link's run.
    An unknown / revoked link (empty or stale pin) is a natural None."""
    repo = JlaunchRunRepo()
    async with scoped_session(maker, jlaunch_share_context(link_id)) as session:
        # The run id isn't known to the visitor; the keystone policy admits exactly the one
        # run the pinned link points at, so a bare list returns it (or nothing).
        rows = await repo.list(session)
    return rows[0] if rows else None
