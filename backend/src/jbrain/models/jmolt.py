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

from sqlalchemy import DateTime, Integer, Text, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base

# Ratified quota (JMOLT_PLAN §6.6). Bytes are UTF-8 byte length, not char count.
MAX_FILES = 16
MAX_TOTAL_BYTES = 128 * 1024
MAX_FILE_BYTES = 24 * 1024
# jmolt's journal (the line to its human): a single entry is capped so one night can't dump
# a novel into the digest, and the table keeps only the most recent N so it stays bounded.
MAX_JOURNAL_BYTES = 8 * 1024
JOURNAL_RETENTION = 200
# Archive retention (M13 — "bounded"): keep the last N snapshots per file.
#
# Raised from 25 with the arrival of append mode. The archive is the ONLY recovery net for a
# scratchpad write that went wrong, and a measured night rewrote one file ten times — at 25
# that was already down to ~2.5 nights of history for an active file. Append makes an edit
# cheap, so the rewrite rate goes up, not down; raising this in the same change is what keeps
# the net the same size in nights rather than in rows.
ARCHIVE_RETENTION = 60
# Renaming carries a file's recent history onto the new name (a rename is a retitle, and the
# versions before it are still the same file's). Bounded deliberately: the per-file prune
# cannot see across names, so an unbounded copy would give a rename loop a multiplier.
RENAME_HISTORY_CARRY = 5
# The per-file prune bounds rows per NAME; renaming grows the set of names. This bounds the
# other axis. 1200 * MAX_FILE_BYTES ≈ 28 MB worst case, and a real night is a few hundred KB.
ARCHIVE_MAX_ROWS_PER_PRINCIPAL = 1200


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


@dataclass(frozen=True)
class JournalEntry:
    content: str
    created_at: datetime


class JmoltJournalRepo:
    """jmolt's append-only journal to its human (migration 0176). Same RLS shape as the
    scratchpad: a jmolt-auth session appends, a jmolt-domain session reads, no one edits.
    Methods run on a caller-supplied, already-scoped `AsyncSession`."""

    async def add(self, session: AsyncSession, principal_id: str, content: str) -> None:
        """Append one journal entry (truncated to the per-entry cap), then prune to the
        most recent JOURNAL_RETENTION rows so the table stays bounded (M13-style). A blank
        entry is a no-op — jmolt not writing tonight is not an empty line in the digest."""
        content = content.strip()
        if not content:
            return
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_JOURNAL_BYTES:
            content = encoded[:MAX_JOURNAL_BYTES].decode("utf-8", "ignore").rstrip()
        await session.execute(
            text("INSERT INTO app.jmolt_journal (principal_id, content) VALUES (:pid, :content)"),
            {"pid": principal_id, "content": content},
        )
        await session.execute(
            text(
                "DELETE FROM app.jmolt_journal WHERE principal_id = :pid AND seq NOT IN ("
                "   SELECT seq FROM app.jmolt_journal WHERE principal_id = :pid"
                "   ORDER BY seq DESC LIMIT :keep"
                " )"
            ),
            {"pid": principal_id, "keep": JOURNAL_RETENTION},
        )

    async def recent(
        self, session: AsyncSession, principal_id: str, limit: int = 30
    ) -> list[JournalEntry]:
        """The journal, newest first — for the morning digest and the PWA journal card."""
        rows = (
            await session.execute(
                text(
                    "SELECT content, created_at FROM app.jmolt_journal"
                    " WHERE principal_id = :pid ORDER BY seq DESC LIMIT :lim"
                ),
                {"pid": principal_id, "lim": limit},
            )
        ).all()
        return [JournalEntry(content=r[0], created_at=r[1]) for r in rows]


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
        the archive. Raises QuotaError (with a plain-language budget message) on violation.

        The quota check (list_files → check → upsert) is not atomic, so two CONCURRENT
        writes of different new files could both pass — but jmolt's nightly run is
        single-flight (`SingleFlightLane`) and its ReAct loop issues tool calls
        sequentially, so no concurrency arises on the intended path; strict enforcement
        (an advisory lock) is unnecessary here."""
        filename = filename.strip()
        if not filename:
            raise QuotaError("a filename is required.")
        new_bytes = await self._check_quota(session, principal_id, filename, content)
        # M13 dedup — snapshot only on change: an identical rewrite adds no archive row, so
        # a loop that keeps saving the same content grows nothing.
        # (`read` returns None for a file that does not exist, so this is also the
        # "already stored identically" test without a second existence lookup.)
        #
        # It still TOUCHES `updated_at`. Returning outright left the row untouched while the
        # tool answered "Saved 'x.md'." — so jmolt saving a file byte-identically was told it
        # had saved and then, next sitting, told by the prologue that nothing had saved all
        # night (`jmolt_night._wrote_tonight` reads `updated_at`). The dedup is about the
        # ARCHIVE, and a write that happened should say when it happened.
        if await self.read(session, principal_id, filename) == content:
            await session.execute(
                update(JmoltScratch)
                .where(
                    JmoltScratch.principal_id == principal_id,
                    JmoltScratch.filename == filename,
                )
                .values(updated_at=func.now())
            )
            return
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

    async def _check_quota(
        self, session: AsyncSession, principal_id: str, filename: str, content: str
    ) -> int:
        """Raise QuotaError if `content` cannot be stored as `filename`. Returns its byte
        length. Shared by every write path so append and save are held to one budget."""
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
        return new_bytes

    async def append(
        self, session: AsyncSession, principal_id: str, filename: str, delta: str
    ) -> tuple[int, int]:
        """Add `delta` to the end of a file, creating it if absent. Returns (prior, new)
        byte lengths.

        This exists because there was no way to add a line to a note without resending the
        whole file: every edit was a full rewrite, which is how a truncated tool call becomes
        a silently emptied file (the reason `write` now refuses an absent content key). The
        quota is checked against the COMBINED content, so append cannot be used to walk past
        a budget a save would have refused."""
        filename = filename.strip()
        if not filename:
            raise QuotaError("a filename is required.")
        prior = await self.read(session, principal_id, filename) or ""
        # A newline between the old tail and the new text unless one is already there —
        # appending to notes, not concatenating a byte stream.
        joiner = "" if not prior or prior.endswith("\n") else "\n"
        content = prior + joiner + delta
        new_bytes = await self._check_quota(session, principal_id, filename, content)
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
        await self._archive(session, principal_id, filename, content, new_bytes, "append")
        return len(prior.encode("utf-8")), new_bytes

    async def rename(self, session: AsyncSession, principal_id: str, old: str, new: str) -> None:
        """Retitle a file, carrying its recent history onto the new name.

        Both prologues tell jmolt to retitle a file that has outgrown its name, and until now
        the only way to do it was read → write-new → delete-old, which orphans the old name's
        archive rows from the file they belong to. A rename does not change the file count, so
        it is deliberately not held to MAX_FILES — refusing a retitle at the file cap would
        make the cap unescapable in exactly the situation the prologue asks for.

        History is COPIED, not moved: `jmolt_scratch_archive` has INSERT and DELETE policies
        and no UPDATE policy, so append-only is a Postgres guarantee rather than a convention
        here, and rewriting a row's filename would need a policy that weakens it."""
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise QuotaError("a rename needs both the current filename and the new one.")
        if old == new:
            raise QuotaError(f"{old!r} is already called that.")
        content = await self.read(session, principal_id, old)
        if content is None:
            raise QuotaError(f"you have no file named {old!r}.")
        if await self.read(session, principal_id, new) is not None:
            raise QuotaError(
                f"you already have a file named {new!r} — renaming onto it would destroy it. "
                "Pick another name, or delete that one first if you meant to replace it."
            )
        nbytes = len(content.encode("utf-8"))
        await session.execute(
            text(
                "UPDATE app.jmolt_scratch SET filename = :new, updated_at = now()"
                " WHERE principal_id = :pid AND filename = :old"
            ),
            {"pid": principal_id, "old": old, "new": new},
        )
        # Select the newest N, then INSERT them OLDEST-FIRST. The copies get fresh `seq`
        # values in insertion order, and `history()` reads newest-first by `seq` — so
        # inserting in the order the inner query returns them (newest first) would give the
        # newest version the LOWEST new seq and invert the file's history. A renamed file
        # would then answer `version=1` with its oldest content, which is precisely the
        # recovery path this wave exists to provide.
        await session.execute(
            text(
                "INSERT INTO app.jmolt_scratch_archive"
                " (principal_id, filename, content, bytes, op)"
                " SELECT principal_id, :new, content, bytes, op FROM ("
                "   SELECT principal_id, content, bytes, op, seq"
                "   FROM app.jmolt_scratch_archive"
                "   WHERE principal_id = :pid AND filename = :old"
                "   ORDER BY seq DESC LIMIT :carry"
                " ) recent ORDER BY recent.seq ASC"
            ),
            {"pid": principal_id, "old": old, "new": new, "carry": RENAME_HISTORY_CARRY},
        )
        await self._archive(session, principal_id, new, content, nbytes, "rename")

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
        # M13 retention — keep only the last ARCHIVE_RETENTION snapshots for this file, so
        # the append-only archive stays bounded. Deletes the oldest overflow only; recent
        # history (the rollback + science window) is untouched.
        await session.execute(
            text(
                "DELETE FROM app.jmolt_scratch_archive"
                " WHERE principal_id = :pid AND filename = :fn AND seq NOT IN ("
                "   SELECT seq FROM app.jmolt_scratch_archive"
                "   WHERE principal_id = :pid AND filename = :fn"
                "   ORDER BY seq DESC LIMIT :keep"
                " )"
            ),
            {"pid": principal_id, "fn": filename, "keep": ARCHIVE_RETENTION},
        )
        # The per-file prune bounds rows per NAME. Renaming mints names, so bound the whole
        # principal's archive too — otherwise a rename loop grows the table without ever
        # tripping a per-file cap.
        await session.execute(
            text(
                "DELETE FROM app.jmolt_scratch_archive"
                " WHERE principal_id = :pid AND seq NOT IN ("
                "   SELECT seq FROM app.jmolt_scratch_archive"
                "   WHERE principal_id = :pid ORDER BY seq DESC LIMIT :keep"
                " )"
            ),
            {"pid": principal_id, "keep": ARCHIVE_MAX_ROWS_PER_PRINCIPAL},
        )
