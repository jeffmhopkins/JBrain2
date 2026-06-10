"""SQL legs of hybrid search. RLS scoping rides the session context: the
chunks/notes policies already hide other-domain rows, so the optional domain
parameter only narrows within what the principal can see anyway."""

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import vector_literal
from jbrain.search.service import ChunkHit

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
                await session.execute(
                    text(_FTS_SQL), {"q": q, "domain": domain, "limit": limit}
                )
            ).all()
        return [_hit(r, headline=r.headline) for r in rows]
