"""jcode session-index ORM + repo (migration 0098).

`jcode_sessions` is the owner-only launcher index for code mode (mirrors
`archivist_memory`/`generated_images`): one row per sandboxed coding session,
holding the repo/branch/status the launcher lists and resumes. The session's real
state — the transcript and the checkout — lives in the jcode control server; this
table is the durable metadata the PWA reads. NOT the owner's knowledge base; no
domain, no notes/entities.

`JcodeSessionRepo` takes the caller's already-RLS-scoped `AsyncSession` directly, so
the owner-only firewall is Postgres', not these methods'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, Text, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class JcodeSession(Base):
    """One sandboxed coding session's launcher metadata."""

    __tablename__ = "jcode_sessions"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    repo: Mapped[str] = mapped_column(Text, default="")
    branch: Mapped[str] = mapped_column(Text, default="main")
    work_branch: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


@dataclass(frozen=True)
class JcodeSessionRow:
    """A plain view of a session row, safe to return from a route."""

    id: str
    repo: str
    branch: str
    work_branch: str
    status: str
    created_at: str
    last_active_at: str

    @classmethod
    def of(cls, row: JcodeSession) -> JcodeSessionRow:
        return cls(
            id=row.id,
            repo=row.repo,
            branch=row.branch,
            work_branch=row.work_branch,
            status=row.status,
            created_at=row.created_at.isoformat(),
            last_active_at=row.last_active_at.isoformat(),
        )


class JcodeSessionRepo:
    """CRUD for the owner's session index on a caller-supplied RLS-scoped session."""

    async def upsert(
        self,
        session: AsyncSession,
        *,
        id: str,
        repo: str,
        branch: str,
        work_branch: str,
        status: str,
    ) -> None:
        stmt = (
            pg_insert(JcodeSession)
            .values(
                id=id,
                repo=repo,
                branch=branch,
                work_branch=work_branch,
                status=status,
                last_active_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=[JcodeSession.id],
                set_={"status": status, "last_active_at": func.now()},
            )
        )
        await session.execute(stmt)

    async def list(self, session: AsyncSession) -> list[JcodeSessionRow]:
        result = await session.execute(
            select(JcodeSession).order_by(JcodeSession.last_active_at.desc())
        )
        return [JcodeSessionRow.of(r) for r in result.scalars()]

    async def get(self, session: AsyncSession, sid: str) -> JcodeSessionRow | None:
        row = await session.get(JcodeSession, sid)
        return JcodeSessionRow.of(row) if row else None

    async def touch(self, session: AsyncSession, sid: str, *, status: str) -> None:
        stmt = (
            pg_insert(JcodeSession)
            .values(id=sid, status=status, last_active_at=func.now())
            .on_conflict_do_update(
                index_elements=[JcodeSession.id],
                set_={"status": status, "last_active_at": func.now()},
            )
        )
        await session.execute(stmt)

    async def delete(self, session: AsyncSession, sid: str) -> None:
        await session.execute(delete(JcodeSession).where(JcodeSession.id == sid))
