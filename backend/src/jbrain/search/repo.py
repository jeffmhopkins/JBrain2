"""SQL legs of hybrid search. RLS scoping rides the session context: the
chunks/notes policies already hide other-domain rows, so the optional domain
parameter only narrows within what the principal can see anyway."""

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import vector_literal
from jbrain.search.service import ChunkHit, WikiHit

# Both legs join notes for deletion filtering + result metadata; the
# attachment count rides along to spare the API a second query.
_SELECT = """
    SELECT c.id AS chunk_id, c.note_id, c.text, c.source_kind, c.source_anchor,
           c.domain_code, n.destination, n.created_at, n.body,
           (SELECT count(*) FROM app.attachments a WHERE a.note_id = n.id)
               AS attachment_count{extra}
    FROM app.chunks c
    JOIN app.notes n ON n.id = c.note_id
    WHERE n.deleted_at IS NULL
      -- Derived chunks are per-domain citation backing (analysis "Mixed-domain
      -- notes"), not primary sources: skip them so the same text a note already
      -- carries in its capture domain is never surfaced twice.
      AND c.source_kind != 'derived'
      AND (cast(:domain AS text) IS NULL OR c.domain_code = cast(:domain AS text))
"""

_DENSE_SQL = (
    _SELECT.format(extra="")
    + """
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> cast(:qvec AS vector), c.id
    LIMIT :limit
"""
)

_FTS_SQL = (
    _SELECT.format(
        extra=""",
           ts_headline('english', c.text, websearch_to_tsquery('english', :q),
                       'StartSel=<mark>, StopSel=</mark>, MaxFragments=2') AS headline"""
    )
    + """
      AND c.tsv @@ websearch_to_tsquery('english', :q)
    ORDER BY ts_rank(c.tsv, websearch_to_tsquery('english', :q)) DESC, c.id
    LIMIT :limit
"""
)


# The wiki leg (Phase-6 §5b). Every query runs in the RLS-scoped session: wiki_index /
# wiki_sections are owner + domain-scoped and wiki_revisions inherits its section's visibility,
# so an out-of-scope section never ranks or leaks via ordering. Only the live revision and active
# (non-redirect/archived) articles are searchable; the article shell carries the display identity.
_WIKI_BASE = """
    FROM app.wiki_sections s
    JOIN app.wiki_articles a ON a.id = s.article_id AND a.status = 'active'
    LEFT JOIN app.wiki_revisions wr ON wr.id = s.current_revision_id
    WHERE (cast(:domain AS text) IS NULL OR s.domain_code = cast(:domain AS text))
"""

_WIKI_DENSE_SQL = """
    SELECT s.article_id, s.id AS section_id, s.domain_code, a.title, a.kind,
           a.lead_summary, coalesce(wr.body, '') AS body
    FROM app.wiki_index wi
    JOIN app.wiki_sections s ON s.id = wi.section_id
    JOIN app.wiki_articles a ON a.id = s.article_id AND a.status = 'active'
    LEFT JOIN app.wiki_revisions wr ON wr.id = s.current_revision_id
    WHERE (cast(:domain AS text) IS NULL OR s.domain_code = cast(:domain AS text))
      AND wi.summary_embedding IS NOT NULL
    ORDER BY wi.summary_embedding <=> cast(:qvec AS vector), s.id
    LIMIT :limit
"""

_WIKI_FTS_SQL = (
    """
    SELECT s.article_id, s.id AS section_id, s.domain_code, a.title, a.kind,
           a.lead_summary, coalesce(wr.body, '') AS body,
           ts_headline('english', coalesce(wr.body, ''),
                       websearch_to_tsquery('english', :q),
                       'StartSel=<mark>, StopSel=</mark>, MaxFragments=2') AS headline
"""
    + _WIKI_BASE
    + """
      AND wr.body_tsv @@ websearch_to_tsquery('english', :q)
    ORDER BY ts_rank(wr.body_tsv, websearch_to_tsquery('english', :q)) DESC, s.id
    LIMIT :limit
"""
)


def _wiki_hit(row: Row[Any], headline: str | None = None) -> WikiHit:
    return WikiHit(
        article_id=str(row.article_id),
        section_id=str(row.section_id),
        title=row.title,
        blurb=row.lead_summary or "",
        entity_kind=row.kind,
        domain=row.domain_code,
        text=row.body,
        headline=headline,
    )


def _hit(row: Row[Any], headline: str | None = None) -> ChunkHit:
    return ChunkHit(
        chunk_id=str(row.chunk_id),
        note_id=str(row.note_id),
        text=row.text,
        source_kind=row.source_kind,
        source_anchor=row.source_anchor,
        domain=row.domain_code,
        destination=row.destination,
        created_at=row.created_at,
        body=row.body,
        attachment_count=row.attachment_count,
        headline=headline,
    )


class SqlSearchRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def dense_search(
        self, ctx: SessionContext, qvec: list[float], domain: str | None, limit: int
    ) -> list[ChunkHit]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(_DENSE_SQL),
                    {"qvec": vector_literal(qvec), "domain": domain, "limit": limit},
                )
            ).all()
        return [_hit(r) for r in rows]

    async def fts_search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> list[ChunkHit]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(text(_FTS_SQL), {"q": q, "domain": domain, "limit": limit})
            ).all()
        return [_hit(r, headline=r.headline) for r in rows]

    async def wiki_dense_search(
        self, ctx: SessionContext, qvec: list[float], domain: str | None, limit: int
    ) -> list[WikiHit]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(_WIKI_DENSE_SQL),
                    {"qvec": vector_literal(qvec), "domain": domain, "limit": limit},
                )
            ).all()
        return [_wiki_hit(r) for r in rows]

    async def wiki_fts_search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> list[WikiHit]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(_WIKI_FTS_SQL), {"q": q, "domain": domain, "limit": limit}
                )
            ).all()
        return [_wiki_hit(r, headline=r.headline) for r in rows]
