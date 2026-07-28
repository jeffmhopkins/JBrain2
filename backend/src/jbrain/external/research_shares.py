"""Public share links for research reports: mint / list / revoke (owner) + resolve (public).

The single writer + reader for `app.research_share_links` (0150). A link targets ONE report
or ONE report folder; the owner mints it (secret shown once, only its SHA-256 hash stored),
lists and revokes it under the full-owner context, and a public visitor resolves the token to
read the target. The read itself is enforced in RLS: `resolve_share` looks the link up by hash
under the empty-scope share context, then the API re-opens PINNED to the link id
(`research_share_context(link_id)`) so `research_reports_share` (0150) admits exactly that
link's report — or its folder's current members — and nothing else.

The public payload's `sources` are sanitized to external URLs only: a `library`/`library_first`
report can carry note-derived citation entries (no URL, private titles/snippets), which must
never ship to a share visitor.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth import keys
from jbrain.db.session import SessionContext, research_share_context, scoped_session
from jbrain.external import research_corpus
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()

# Source modes whose report body synthesizes the owner's private notes — the owner is warned
# before sharing one (report link) or a folder that contains one (group link).
_LIBRARY_MODES = ("library", "library_first")


@dataclass(frozen=True)
class ShareLink:
    """One share link in the owner's management list — its target + usage, no secret."""

    id: str
    label: str | None
    target_kind: str
    report_id: str | None
    group_id: str | None
    created_at: datetime
    revoked_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int
    # True when the shared target is (report link) or contains (group link) a report drawn
    # from the owner's private notes — the list badges it (private-notes risk). Computed in
    # `list_shares`; absent from a bare mint row (the API sets the mint warning directly).
    library_warning: bool = False


@dataclass(frozen=True)
class ResolvedShare:
    """A validated (non-revoked) link resolved from a token — the pin + target for the read."""

    id: str
    target_kind: str
    label: str | None
    report_id: str | None
    group_id: str | None


_LINK_FIELDS = (
    "id, label, target_kind, report_id, group_id, created_at,"
    " revoked_at, last_viewed_at, view_count"
)
_LINK_COLS = ", ".join(f"l.{c.strip()}" for c in _LINK_FIELDS.split(","))


def _row_to_link(row: Any) -> ShareLink:
    return ShareLink(
        id=str(row.id),
        label=row.label,
        target_kind=row.target_kind,
        report_id=(str(row.report_id) if row.report_id is not None else None),
        group_id=(str(row.group_id) if row.group_id is not None else None),
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_viewed_at=row.last_viewed_at,
        view_count=int(row.view_count or 0),
        library_warning=bool(getattr(row, "library_warning", False)),
    )


async def mint_share(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    target_kind: str,
    report_id: str | None,
    group_id: str | None,
    label: str | None,
) -> tuple[str, ShareLink]:
    """Mint a link for a report or folder under the OWNER's context; returns the bearer secret
    exactly once alongside its record. Only the secret's SHA-256 is stored. The caller validates
    that the target is in the owner's scope and snapshots the folder name into `label`."""
    secret = keys.generate_capability_key()
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.research_share_links"
                    " (token_hash, label, target_kind, report_id, group_id, principal_id)"
                    " VALUES (:h, :label, :kind, cast(:rid AS uuid), cast(:gid AS uuid),"
                    "  cast(:pid AS uuid))"
                    f" RETURNING {_LINK_FIELDS}"
                ),
                {
                    "h": keys.hash_token(secret),
                    "label": label,
                    "kind": target_kind,
                    "rid": report_id,
                    "gid": group_id,
                    "pid": ctx.principal_id,
                },
            )
        ).one()
    link = _row_to_link(row)
    log.info("research_share_mint", share_id=link.id, target_kind=target_kind)
    return secret, link


async def list_shares(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> list[ShareLink]:
    """The owner's live (non-revoked) share links, newest first — metadata only, no secrets.
    `library_warning` is true when a report link's target — or ANY current member of a folder
    link — was drawn from private notes, so the list flags either kind."""
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_LINK_COLS},"
                    " CASE WHEN l.report_id IS NOT NULL"
                    "   THEN coalesce(r.source_mode = ANY(:modes), false)"
                    "   ELSE EXISTS (SELECT 1 FROM app.research_reports g"
                    "     WHERE g.group_id = l.group_id AND g.status = 'done'"
                    "       AND g.source_mode = ANY(:modes))"
                    " END AS library_warning"
                    " FROM app.research_share_links l"
                    " LEFT JOIN app.research_reports r ON r.id = l.report_id"
                    " WHERE l.revoked_at IS NULL ORDER BY l.created_at DESC, l.id"
                ),
                {"modes": list(_LIBRARY_MODES)},
            )
        ).all()
    return [_row_to_link(r) for r in rows]


async def revoke_share(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, share_id: str
) -> bool:
    """Revoke a link (owner). True when a live link was actually revoked; a non-uuid /
    unknown / already-revoked id is a clean False. Fails the public read closed at once
    (the RLS policy re-checks `revoked_at IS NULL` on every visit)."""
    try:
        uuid.UUID(share_id)
    except ValueError:
        return False
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "UPDATE app.research_share_links SET revoked_at = now()"
                    " WHERE id = cast(:id AS uuid) AND revoked_at IS NULL RETURNING id"
                ),
                {"id": share_id},
            )
        ).first()
    if row is not None:
        log.info("research_share_revoke", share_id=share_id)
    return row is not None


async def resolve_share(
    maker: async_sessionmaker[AsyncSession], token: str
) -> ResolvedShare | None:
    """Resolve a bearer token to its live link, or None (empty/unknown/revoked). Reads by hash
    under the no-pin share context (the `research_share_links_public_read` policy), then bumps
    the view counter best-effort under the owner SYSTEM context (the visitor has no write)."""
    token = (token or "").strip()
    if not token:
        return None
    async with scoped_session(maker, research_share_context()) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, target_kind, label, report_id, group_id"
                    " FROM app.research_share_links"
                    " WHERE token_hash = :h AND revoked_at IS NULL"
                ),
                {"h": keys.hash_token(token)},
            )
        ).first()
    if row is None:
        return None
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "UPDATE app.research_share_links"
                " SET view_count = view_count + 1, last_viewed_at = now()"
                " WHERE id = :id"
            ),
            {"id": str(row.id)},
        )
    return ResolvedShare(
        id=str(row.id),
        target_kind=row.target_kind,
        label=row.label,
        report_id=(str(row.report_id) if row.report_id is not None else None),
        group_id=(str(row.group_id) if row.group_id is not None else None),
    )


async def group_has_library_report(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, group_id: str
) -> bool:
    """Whether folder `group_id` currently contains a report drawn from private notes — the
    mint-time warning for a folder link. Runs under the owner ctx (reads the `external` corpus)."""
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.research_reports WHERE group_id = cast(:gid AS uuid)"
                    " AND status = 'done' AND source_mode = ANY(:modes) LIMIT 1"
                ),
                {"gid": group_id, "modes": list(_LIBRARY_MODES)},
            )
        ).first()
    return row is not None


def _public_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project each external-URL citation to `{url, title}` ONLY — an explicit allowlist, not a
    passthrough. A library report's `sources` can carry note-derived entries (private
    titles/snippets, no URL, or a private field beside a real URL) that must never reach a share
    visitor; a web report's citations are `{url, title}` already, so this is lossless there."""
    kept: list[dict[str, Any]] = []
    for source in sources:
        url = str(source.get("url") or "")
        if url.startswith("http://") or url.startswith("https://"):
            kept.append({"url": url, "title": str(source.get("title") or "")})
    return kept


async def fetch_shared_report(
    maker: async_sessionmaker[AsyncSession], link_id: str, report_id: str
) -> research_corpus.ReportRecord | None:
    """One report of a share, read PINNED to `link_id` so RLS admits only the link's target
    report (or, for a folder link, a current member). `sources` are sanitized for the public
    payload. An out-of-scope / non-uuid `report_id` is a natural None (→ 404)."""
    record = await research_corpus.fetch_report_scoped(
        maker, research_share_context(link_id), report_id
    )
    if record is None:
        return None
    return dataclasses.replace(record, sources=_public_sources(record.sources))


async def list_group_reports(
    maker: async_sessionmaker[AsyncSession], link_id: str
) -> list[research_corpus.LibraryReport]:
    """The reports a folder link currently exposes — read PINNED to `link_id`, so RLS returns
    exactly the shared folder's live members (dynamic: reports moved in/out appear/vanish)."""
    reports = await research_corpus.list_reports_scoped(maker, research_share_context(link_id))
    if len(reports) >= research_corpus.SCOPED_LIST_LIMIT:
        # A folder is assumed browse-sized; flag if a share ever hits the cap (silent
        # truncation would read as "the whole folder" when it isn't).
        log.warning("research_share_group_index_truncated", link_id=link_id, count=len(reports))
    return reports
