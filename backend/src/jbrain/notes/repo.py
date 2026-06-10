"""SQL notes repository. Every query runs on an RLS-scoped session, so
domain filtering is enforced by Postgres, not by these methods."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.notes import Attachment, Note
from jbrain.notes.service import AttachmentInfo, NoteInfo, UnknownDomain


def _attachment_info(a: Attachment) -> AttachmentInfo:
    return AttachmentInfo(
        id=str(a.id),
        filename=a.filename,
        media_type=a.media_type,
        size_bytes=a.size_bytes,
        sha256=a.sha256,
    )


def _note_info(n: Note) -> NoteInfo:
    return NoteInfo(
        id=str(n.id),
        client_id=n.client_id,
        domain=n.domain_code,
        destination=n.destination,
        body=n.body,
        created_at=n.created_at,
        ingest_state=n.ingest_state,
        attachments=[_attachment_info(a) for a in n.attachments],
    )


class SqlNotesRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def create_note(
        self,
        ctx: SessionContext,
        *,
        client_id: str,
        domain: str,
        destination: str | None,
        body: str,
    ) -> tuple[NoteInfo, bool]:
        try:
            async with scoped_session(self._maker, ctx) as session:
                note = Note(
                    client_id=client_id, domain_code=domain, destination=destination, body=body
                )
                session.add(note)
                await session.flush()
                await session.refresh(note)
                return _note_info(note), True
        except IntegrityError as exc:
            # Unique client_id makes offline retries idempotent; FK failures
            # mean the domain code is bogus.
            if "client_id" not in str(exc.orig):
                raise UnknownDomain(domain) from exc
        async with scoped_session(self._maker, ctx) as session:
            existing = (
                await session.execute(select(Note).where(Note.client_id == client_id))
            ).scalar_one()
            return _note_info(existing), False

    async def list_notes(
        self, ctx: SessionContext, *, limit: int, before: datetime | None
    ) -> list[NoteInfo]:
        async with scoped_session(self._maker, ctx) as session:
            query = (
                select(Note)
                .where(Note.deleted_at.is_(None))
                .order_by(Note.created_at.desc(), Note.id.desc())
                .limit(limit)
            )
            if before is not None:
                query = query.where(Note.created_at < before)
            rows = (await session.execute(query)).scalars().all()
            return [_note_info(n) for n in rows]

    async def add_attachment(
        self,
        ctx: SessionContext,
        *,
        note_id: str,
        sha256: str,
        filename: str,
        media_type: str,
        size_bytes: int,
    ) -> AttachmentInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(select(Note).where(Note.id == note_id))
            ).scalar_one_or_none()
            if note is None:
                return None
            attachment = Attachment(
                note_id=note.id,
                domain_code=note.domain_code,
                sha256=sha256,
                filename=filename,
                media_type=media_type,
                size_bytes=size_bytes,
            )
            session.add(attachment)
            await session.flush()
            await session.refresh(attachment)
            return _attachment_info(attachment)

    async def get_attachment(
        self, ctx: SessionContext, attachment_id: str
    ) -> AttachmentInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(select(Attachment).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            return None if row is None else _attachment_info(row)
