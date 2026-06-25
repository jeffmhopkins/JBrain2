"""Archivist memory ORM + repo (migration 0094).

`archivist_memory` is an owner-only scratchpad (mirrors `generated_images`/`wiki_*`):
one row per principal holding the archivist persona's cross-session working notes —
its label taxonomy, filing rules, and progress. NOT the owner's knowledge base; no
domain, no notes/entities.

`ArchivistMemoryRepo` takes the caller's already-RLS-scoped `AsyncSession` directly
(the handler owns the session/transaction), so the owner-only firewall is Postgres',
not these methods'.
"""

from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class ArchivistMemory(Base):
    """The archivist's private notes for one principal — read at session start,
    replaced on each write. `content` is freeform agent-authored text."""

    __tablename__ = "archivist_memory"
    __table_args__ = {"schema": "app"}

    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ArchivistMemoryRepo:
    """Reads/writes the owner's archivist-memory row on a caller-supplied RLS-scoped
    session. A write is a full replace (upsert), matching the read-then-write-merged
    model the tool prompt describes."""

    async def read(self, session: AsyncSession, principal_id: str) -> str:
        row = await session.get(ArchivistMemory, principal_id)
        return row.content if row else ""

    async def write(self, session: AsyncSession, principal_id: str, content: str) -> None:
        stmt = (
            pg_insert(ArchivistMemory)
            .values(principal_id=principal_id, content=content, updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[ArchivistMemory.principal_id],
                set_={"content": content, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
