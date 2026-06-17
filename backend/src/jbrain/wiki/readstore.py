"""Wiki read assembly (Phase 6 — the reader/landing read APIs).

Assembles the frontend's `WikiArticleOut` / `WikiLandingOut` shapes from the wiki tables, every
query on the principal's RLS-scoped session: `wiki_articles` is owner-only and sections/revisions/
citations are domain-scoped, so an out-of-scope section (and its references) never appears in a
rendered article, and a scoped principal only ever sees articles/links it may. The stored section
body is prose carrying inline `[n]` markers (the builder's output); the reader renders it as a
single paragraph block and resolves the `[n]`s against the References list assembled from
`wiki_citations`. Richer typed blocks (lists/tables) and infobox fields are a later enhancement.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

SNIPPET_CHARS = 160
RECENT_LIMIT = 12
HUB_LIMIT = 10

# The References list is assembled from section citations only, so a stray [n] in the lead blurb
# would render an orphan (dead) citation button — strip them from the lead defensively.
_CITE_MARKER = re.compile(r"\[\d+\]")

# Canonical entity kind → the landing's plural group label (mirrors the frontend type families).
_GROUP = {
    "person": "People",
    "people": "People",
    "patient": "People",
    "organization": "Organizations",
    "organisation": "Organizations",
    "company": "Organizations",
    "institution": "Organizations",
    "clinic": "Organizations",
    "hospital": "Organizations",
    "place": "Places",
    "location": "Places",
    "city": "Places",
    "event": "Events",
    "product": "Products",
    "drug": "Medical",
    "medicalcondition": "Medical",
    "medicalprocedure": "Medical",
}


def _group_label(kind: str) -> str:
    return _GROUP.get("".join(c for c in kind.lower() if c.isalnum()), "Other")


def _truncate(s: str, limit: int = SNIPPET_CHARS) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _when(moment: datetime, now: datetime) -> str:
    """A coarse relative label for the 'recently updated' rail."""
    delta = now - moment
    secs = delta.total_seconds()
    if secs < 3600:
        return "just now" if secs < 120 else f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


class WikiReadStore:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def get_article(self, ctx: SessionContext, article_id: str) -> dict[str, Any] | None:
        try:
            aid = str(uuid.UUID(article_id))
        except ValueError:
            return None
        async with scoped_session(self._maker, ctx) as session:
            art = (
                await session.execute(
                    text(
                        "SELECT id, title, kind, lead_summary, image_sha"
                        " FROM app.wiki_articles WHERE id = :a AND status = 'active'"
                    ),
                    {"a": aid},
                )
            ).first()
            if art is None:
                return None
            top = (
                await session.execute(
                    text(
                        "SELECT s.id, s.heading, s.domain_code, coalesce(r.body, '') AS body"
                        " FROM app.wiki_sections s"
                        " LEFT JOIN app.wiki_revisions r ON r.id = s.current_revision_id"
                        " WHERE s.article_id = :a AND s.parent_section_id IS NULL"
                        " ORDER BY s.seq, s.id"
                    ),
                    {"a": aid},
                )
            ).all()
            sections = []
            for s in top:
                subs = (
                    await session.execute(
                        text(
                            "SELECT s.heading, coalesce(r.body, '') AS body"
                            " FROM app.wiki_sections s"
                            " LEFT JOIN app.wiki_revisions r ON r.id = s.current_revision_id"
                            " WHERE s.parent_section_id = :p ORDER BY s.seq, s.id"
                        ),
                        {"p": s.id},
                    )
                ).all()
                section: dict[str, Any] = {
                    "heading": s.heading,
                    "domain": s.domain_code,
                    "blocks": _blocks(s.body),
                }
                if subs:
                    section["subsections"] = [
                        {"heading": sub.heading, "blocks": _blocks(sub.body)} for sub in subs
                    ]
                sections.append(section)
            refs = (
                await session.execute(
                    text(
                        "SELECT DISTINCT c.seq, c.note_id, c.domain_code, n.created_at, ch.text"
                        " FROM app.wiki_citations c"
                        " JOIN app.wiki_revisions r ON r.id = c.revision_id"
                        " JOIN app.wiki_sections s ON s.id = r.section_id"
                        "   AND s.article_id = :a AND r.id = s.current_revision_id"
                        " JOIN app.notes n ON n.id = c.note_id"
                        " JOIN app.chunks ch ON ch.id = c.chunk_id"
                        " ORDER BY c.seq"
                    ),
                    {"a": aid},
                )
            ).all()
        kind = art.kind or "Thing"
        return {
            "id": str(art.id),
            "title": art.title,
            "subtitle": f"{kind} · machine-written from your notes",
            "infobox": {
                "title": art.title,
                "kind": kind,
                "photo": bool(art.image_sha),
                "fields": [],
            },
            "lead": (
                [{"kind": "p", "text": _CITE_MARKER.sub("", art.lead_summary).strip()}]
                if art.lead_summary
                else []
            ),
            "sections": sections,
            "references": [
                {
                    "n": r.seq,
                    "note_id": str(r.note_id),
                    "meta": f"Note · {r.created_at:%b %d, %Y}",
                    "domain": r.domain_code,
                    "snippet": _truncate(r.text),
                }
                for r in refs
            ],
        }

    async def get_landing(
        self, ctx: SessionContext, *, now: datetime | None = None
    ) -> dict[str, Any]:
        moment = now or datetime.now(UTC)
        async with scoped_session(self._maker, ctx) as session:
            articles = (
                await session.execute(
                    text(
                        "SELECT id, title, kind, lead_summary, updated_at"
                        " FROM app.wiki_articles WHERE status = 'active' ORDER BY updated_at DESC"
                    )
                )
            ).all()
            hubs = (
                await session.execute(
                    text(
                        # Inbound links count via the soft entity ref the builder populates
                        # (`to_entity_id` = the target's `entity_ref`); the count is post-RLS,
                        # so a scoped principal's hub totals equal their visible links. Only
                        # CROSS-article links count — a link from the article's own section
                        # (a self/reflexive relationship fact) is excluded, else an entity would
                        # be reported as a hub of itself.
                        "SELECT a.id, a.title, a.kind, a.lead_summary,"
                        "   count(l.id) FILTER (WHERE fs.article_id <> a.id) AS links"
                        " FROM app.wiki_articles a"
                        " LEFT JOIN app.wiki_links l ON l.to_entity_id = a.entity_ref"
                        " LEFT JOIN app.wiki_sections fs ON fs.id = l.from_section_id"
                        " WHERE a.status = 'active'"
                        " GROUP BY a.id, a.title, a.kind, a.lead_summary"
                        " HAVING count(l.id) FILTER (WHERE fs.article_id <> a.id) > 0"
                        " ORDER BY count(l.id) FILTER (WHERE fs.article_id <> a.id) DESC, a.title"
                        " LIMIT :lim"
                    ),
                    {"lim": HUB_LIMIT},
                )
            ).all()

        def entry(row: Any) -> dict[str, Any]:
            return {
                "id": str(row.id),
                "title": row.title,
                "kind": row.kind or "Thing",
                "domain": "general",
                "blurb": row.lead_summary or "",
            }

        recent = [
            {**entry(a), "when": _when(a.updated_at, moment)} for a in articles[:RECENT_LIMIT]
        ]
        hub_entries = [{**entry(h), "links": int(h.links)} for h in hubs]

        groups: dict[str, list[dict[str, Any]]] = {}
        for a in articles:
            groups.setdefault(_group_label(a.kind or "Thing"), []).append(entry(a))
        group_list = [
            {"type": label, "entries": sorted(entries, key=lambda e: e["title"].lower())}
            for label, entries in sorted(groups.items())
        ]
        return {"recent": recent, "hubs": hub_entries, "groups": group_list}


def _blocks(body: str) -> list[dict[str, Any]]:
    """The stored prose (with inline [n] markers) as paragraph blocks — one per non-empty line."""
    paras = [ln.strip() for ln in body.splitlines() if ln.strip()] or ([body] if body else [])
    return [{"kind": "p", "text": p} for p in paras]
