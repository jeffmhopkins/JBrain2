"""jmolt scratchpad ORM + repo (migration 0173, docs/plans/JMOLT_PLAN.md W2).

A small set of agent-authored files (`jmolt_scratch`) that are jmolt's only cross-night
memory, plus an append-only `jmolt_scratch_archive` that snapshots every change OUTSIDE
the quota — the audit/rollback trail and the science instrument (M13).

Quota (16 files / 128 KB total / 24 KB per file) is enforced HERE in the write path;
the DB has no quota constraint. RLS (the M19 split) is Postgres' job — these methods run
on a caller-supplied, already-scoped `AsyncSession`: a jmolt-auth-context session may
write, a jmolt-domain-scoped session may read, anyone else is denied by the policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base

# Ratified quota (JMOLT_PLAN §6.6). Bytes are UTF-8 byte length, not char count.
MAX_FILES = 16
MAX_TOTAL_BYTES = 128 * 1024
MAX_FILE_BYTES = 24 * 1024


class QuotaError(Exception):
    """A scratch write that would exceed the quota. The message is owner/agent-facing
    and states the budget plainly."""


class JmoltScratch(Base):
    __tablename__ = "jmolt_scratch"
    __table_args__ = {"schema": "app"}

    id: Mapped[str] = mapped_column(Text, primary_key=True, server_default=func.gen_random_uuid())
    principal_id: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, default="")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class ScratchFile:
    filename: str
    bytes: int
    updated_at: datetime


@dataclass(frozen=True)
class ScratchVersion:
    filename: str
    content: str
    bytes: int
    op: str
    archived_at: datetime


class JmoltScratchRepo:
    """Reads/writes the jmolt scratchpad on a caller-supplied RLS-scoped session."""

    async def list_files(self, session: AsyncSession, principal_id: str) -> list[ScratchFile]:
        rows = (
            await session.execute(
                select(JmoltScratch.filename, JmoltScratch.bytes, JmoltScratch.updated_at)
                .where(JmoltScratch.principal_id == principal_id)
                .order_by(JmoltScratch.filename)
            )
        ).all()
        return [ScratchFile(filename=r[0], bytes=r[1], updated_at=r[2]) for r in rows]

    async def read(self, session: AsyncSession, principal_id: str, filename: str) -> str | None:
        row = (
            await session.execute(
                select(JmoltScratch.content).where(
                    JmoltScratch.principal_id == principal_id,
                    JmoltScratch.filename == filename,
                )
            )
        ).scalar_one_or_none()
        return row

    async def write(
        self, session: AsyncSession, principal_id: str, filename: str, content: str
    ) -> None:
        """Create or overwrite a file, enforcing the quota, and snapshot the new content to
        the archive. Raises QuotaError (with a plain-language budget message) on violation."""
        filename = filename.strip()
        if not filename:
            raise QuotaError("a filename is required.")
        new_bytes = len(content.encode("utf-8"))
        if new_bytes > MAX_FILE_BYTES:
            raise QuotaError(
                f"that file is {new_bytes} bytes; the per-file limit is {MAX_FILE_BYTES}"
                f" ({MAX_FILE_BYTES // 1024} KB). Trim it and save again."
            )
        existing = {f.filename: f.bytes for f in await self.list_files(session, principal_id)}
        others_total = sum(b for name, b in existing.items() if name != filename)
        if filename not in existing and len(existing) >= MAX_FILES:
            raise QuotaError(
                f"you already have {len(existing)} files; the limit is {MAX_FILES}. "
                "Delete one before making a new one."
            )
        if others_total + new_bytes > MAX_TOTAL_BYTES:
            raise QuotaError(
                f"that would put your files over the {MAX_TOTAL_BYTES // 1024} KB total budget "
                f"({others_total + new_bytes} bytes used of {MAX_TOTAL_BYTES}). "
                "Trim or delete something first."
            )
        stmt = (
            pg_insert(JmoltScratch)
            .values(
                principal_id=principal_id,
                filename=filename,
                content=content,
                bytes=new_bytes,
                updated_at=func.now(),
            )
            .on_conflict_do_update(
                index_elements=[JmoltScratch.principal_id, JmoltScratch.filename],
                set_={"content": content, "bytes": new_bytes, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
        await self._archive(session, principal_id, filename, content, new_bytes, "write")

    async def delete(self, session: AsyncSession, principal_id: str, filename: str) -> bool:
        """Delete a file, snapshotting its last content to the archive. Returns whether a
        file was deleted."""
        prior = await self.read(session, principal_id, filename)
        if prior is None:
            return False
        await session.execute(
            text("DELETE FROM app.jmolt_scratch WHERE principal_id = :pid AND filename = :fn"),
            {"pid": principal_id, "fn": filename},
        )
        await self._archive(
            session, principal_id, filename, prior, len(prior.encode("utf-8")), "delete"
        )
        return True

    async def history(
        self, session: AsyncSession, principal_id: str, filename: str | None = None
    ) -> list[ScratchVersion]:
        """The archive (append-only), newest first — for jerv's read-only observation."""
        sql = (
            "SELECT filename, content, bytes, op, archived_at FROM app.jmolt_scratch_archive"
            " WHERE principal_id = :pid"
        )
        params: dict[str, object] = {"pid": principal_id}
        if filename is not None:
            sql += " AND filename = :fn"
            params["fn"] = filename
        sql += " ORDER BY seq DESC LIMIT 200"
        rows = (await session.execute(text(sql), params)).all()
        return [
            ScratchVersion(filename=r[0], content=r[1], bytes=r[2], op=r[3], archived_at=r[4])
            for r in rows
        ]

    async def _archive(
        self,
        session: AsyncSession,
        principal_id: str,
        filename: str,
        content: str,
        nbytes: int,
        op: str,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO app.jmolt_scratch_archive"
                " (principal_id, filename, content, bytes, op)"
                " VALUES (:pid, :fn, :content, :bytes, :op)"
            ),
            {"pid": principal_id, "fn": filename, "content": content, "bytes": nbytes, "op": op},
        )
