"""SQL notes repository. Every query runs on an RLS-scoped session, so
domain filtering is enforced by Postgres, not by these methods."""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.purge import purge_note_artifacts
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.notes import Attachment, AttachmentExtract, Chunk, Note
from jbrain.notes.service import (
    AttachmentInfo,
    ExtractInfo,
    NoteInfo,
    NoteUpdate,
    UnknownDomain,
)


def _attachment_info(a: Attachment) -> AttachmentInfo:
    return AttachmentInfo(
        id=str(a.id),
        filename=a.filename,
        media_type=a.media_type,
        size_bytes=a.size_bytes,
        sha256=a.sha256,
        has_extracts=a.has_extracts,
        has_description=a.has_description,
    )


def _note_info(n: Note) -> NoteInfo:
    return NoteInfo(
        id=str(n.id),
        client_id=n.client_id,
        domain=n.domain_code,
        destination=n.destination,
        body=n.body,
        created_at=n.created_at,
        tz_offset_minutes=n.tz_offset_minutes,
        ingest_state=n.ingest_state,
        hidden=n.hidden_at is not None,
        analyzed=n.analyzed,
        provenance=n.provenance,
        attachments=[_attachment_info(a) for a in n.attachments],
        latitude=n.latitude,
        longitude=n.longitude,
        accuracy_m=n.location_accuracy_m,
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
        created_at: datetime | None = None,
        tz_offset_minutes: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
        provenance: str = "human",
        source_ref: str | None = None,
    ) -> tuple[NoteInfo, bool]:
        # FUTURE: ingestion (`ingest_note`) is enqueued by each caller (the notes
        # API, the proposal executor) rather than here — so a new write path can
        # silently forget it and leave the note stuck at 'pending' (this is how
        # proposal-enacted notes never indexed). Fold the enqueue into a single
        # "note created" trigger (here or a DB notify) so every path indexes
        # uniformly. Needs a JobEnqueuer on the repo (kept out today to keep it
        # storage-only); do it when there's a third write path.
        # Client capture time wins when supplied (the offline outbox flushes
        # later, so server now() would be wrong); omitting the key lets the
        # column's server_default stamp now() instead of writing NULL.
        captured = {"created_at": created_at} if created_at is not None else {}
        try:
            async with scoped_session(self._maker, ctx) as session:
                note = Note(
                    client_id=client_id,
                    domain_code=domain,
                    destination=destination,
                    body=body,
                    tz_offset_minutes=tz_offset_minutes,
                    latitude=latitude,
                    longitude=longitude,
                    location_accuracy_m=accuracy_m,
                    provenance=provenance,
                    source_ref=source_ref,
                    **captured,
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
            # The home stream excludes hidden notes; they live on in Search.
            query = (
                select(Note)
                .where(Note.deleted_at.is_(None), Note.hidden_at.is_(None))
                .order_by(Note.created_at.desc(), Note.id.desc())
                .limit(limit)
            )
            if before is not None:
                query = query.where(Note.created_at < before)
            rows = (await session.execute(query)).scalars().all()
            return [_note_info(n) for n in rows]

    async def update_note(
        self, ctx: SessionContext, note_id: str, changes: NoteUpdate
    ) -> NoteInfo | None:
        try:
            async with scoped_session(self._maker, ctx) as session:
                note = (
                    await session.execute(
                        select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                    )
                ).scalar_one_or_none()
                if note is None:
                    return None
                if changes.body is not None:
                    note.body = changes.body
                if changes.domain is not None and changes.domain != note.domain_code:
                    note.domain_code = changes.domain
                    # Attachments duplicate the note's domain (0002 invariant)
                    # so a domain move must carry them along; chunks re-derive
                    # theirs from the note at re-ingest.
                    await session.execute(
                        update(Attachment)
                        .where(Attachment.note_id == note.id)
                        .values(domain_code=changes.domain)
                    )
                if changes.clear_destination:
                    note.destination = None
                elif changes.destination is not None:
                    note.destination = changes.destination
                note.updated_at = datetime.now(UTC)
                # Any edit invalidates chunks/embeddings: back to 'pending'
                # until the re-enqueued ingest rebuilds them.
                note.ingest_state = "pending"
                await session.flush()
                await session.refresh(note)
                return _note_info(note)
        except IntegrityError as exc:
            raise UnknownDomain(changes.domain or "") from exc

    async def delete_note(self, ctx: SessionContext, note_id: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(
                    select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if note is None:
                return False
            # Soft-delete keeps the note row (settled Phase 2 behavior), but
            # everything DERIVED purges hard in this same transaction —
            # facts, mentions, tokens, review items, the analysis header,
            # orphaned provisional entities — because deleting a note is a
            # privacy promise (jbrain.analysis.purge). Chunks go hard too so
            # the search index never serves a deleted note's text. Only true
            # note deletion comes here; attachment removal and edits
            # re-ingest instead and never purge.
            note.deleted_at = datetime.now(UTC)
            await purge_note_artifacts(session, note.id)
            await session.execute(delete(Chunk).where(Chunk.note_id == note.id))
            return True

    async def set_hidden(self, ctx: SessionContext, note_id: str, hidden: bool) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(
                    select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if note is None:
                return False
            # Visibility only: chunks/embeddings are untouched so the note
            # stays searchable, and ingest_state is not reset.
            note.hidden_at = datetime.now(UTC) if hidden else None
            return True

    async def get_note(self, ctx: SessionContext, note_id: str) -> NoteInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            return None if row is None else _note_info(row)

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

    async def remove_attachment(self, ctx: SessionContext, attachment_id: str) -> str | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(select(Attachment).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            note_id = str(row.note_id)
            # The blob stays: content-addressed storage may share it with
            # other notes; only the link (and, via re-ingest, its chunks) go.
            await session.delete(row)
            return note_id

    async def get_attachment(
        self, ctx: SessionContext, attachment_id: str
    ) -> AttachmentInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(select(Attachment).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            return None if row is None else _attachment_info(row)

    async def list_extracts(
        self, ctx: SessionContext, attachment_id: str
    ) -> list[ExtractInfo] | None:
        async with scoped_session(self._maker, ctx) as session:
            att = (
                await session.execute(select(Attachment.id).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            if att is None:
                return None
            rows = (
                (
                    await session.execute(
                        select(AttachmentExtract)
                        .where(AttachmentExtract.attachment_id == attachment_id)
                        # ocr first, then caption — the expansion's reading order.
                        .order_by(AttachmentExtract.kind.desc(), AttachmentExtract.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                ExtractInfo(
                    kind=r.kind,
                    text=r.text,
                    tool=r.tool,
                    confidence=r.confidence,
                    created_at=r.created_at,
                )
                for r in rows
            ]
