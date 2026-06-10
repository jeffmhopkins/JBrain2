"""Database-side metrics for the Ops screen."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


@dataclass(frozen=True)
class DbStats:
    db_size_bytes: int
    note_count: int
    attachment_count: int
    attachment_bytes: int


async def database_stats(maker: async_sessionmaker[AsyncSession], ctx: SessionContext) -> DbStats:
    async with scoped_session(maker, ctx) as session:
        size = (
            await session.execute(text("SELECT pg_database_size(current_database())"))
        ).scalar_one()
        notes = (
            await session.execute(text("SELECT count(*) FROM app.notes WHERE deleted_at IS NULL"))
        ).scalar_one()
        att = (
            await session.execute(
                text("SELECT count(*), coalesce(sum(size_bytes), 0) FROM app.attachments")
            )
        ).one()
    return DbStats(
        db_size_bytes=size,
        note_count=notes,
        attachment_count=att[0],
        attachment_bytes=att[1],
    )
