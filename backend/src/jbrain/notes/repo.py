"""SQL notes repository. Every query runs on an RLS-scoped session, so
domain filtering is enforced by Postgres, not by these methods."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.purge import purge_note_artifacts
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.notes import Attachment, AttachmentExtract, Chunk, Note, NoteClarification
from jbrain.notes.compose import compose_body, strip_clarifications
from jbrain.notes.service import (
    AttachmentInfo,
    ClarificationInfo,
    ClarificationsAltered,
    ExtractInfo,
    NoteInfo,
    NoteUpdate,
    UnknownDomain,
)
from jbrain.queue import enqueue_on


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
        # The note's TEXT, not the body column: D6's clarification blocks are appended
        # here so the existing note view renders them as text with no frontend change.
        body=compose_body(n.body, n.clarifications),
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
        attachments_expected: int = 0,
        provenance: str = "human",
        source_ref: str | None = None,
        wiki_revision_id: uuid.UUID | None = None,
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
                    attachments_expected=attachments_expected,
                    provenance=provenance,
                    source_ref=source_ref,
                    wiki_revision_id=wiki_revision_id,
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
                    # The editor loads what `_note_info` served — body + clarification
                    # blocks — and PATCHes the whole string back, so an untouched save
                    # would otherwise bake the blocks into the body column and double
                    # them on the next read. Remove exactly the suffix this note's own
                    # rows compose to; a body that merely LOOKS like it carries a block
                    # (pasted from a clarified note, or typed) is left whole, because a
                    # note must never be truncatable by its own text.
                    stripped = strip_clarifications(changes.body, note.clarifications)
                    if stripped is None:
                        raise ClarificationsAltered(note_id)
                    note.body = stripped
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
                    # Clarifications duplicate it for the same reason (0193's policy
                    # takes no join). Left behind they would be invisible to the
                    # note's own domain scope — the note moves, its answers do not.
                    await session.execute(
                        update(NoteClarification)
                        .where(NoteClarification.note_id == note.id)
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

    async def append_clarification(
        self,
        ctx: SessionContext,
        note_id: str,
        *,
        question: str,
        answer: str,
        session_id: str | None = None,
    ) -> NoteInfo | None:
        return await self.append_clarifications(
            ctx, note_id, pairs=[(question, answer)], session_id=session_id
        )

    async def append_clarifications(
        self,
        ctx: SessionContext,
        note_id: str,
        *,
        pairs: Sequence[tuple[str, str]],
        session_id: str | None = None,
    ) -> NoteInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(
                    select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if note is None:
                return None
            if not pairs:
                # Nothing was appended, so the note's text did not change: flipping
                # `ingest_state` and queueing a re-ingest here would re-chunk a note
                # nobody edited. A reply whose every answer was dropped lands here.
                return _note_info(note)
            for question, answer in pairs:
                session.add(
                    NoteClarification(
                        note_id=note.id,
                        question=question,
                        answer=answer,
                        session_id=uuid.UUID(session_id) if session_id else None,
                        domain_code=note.domain_code,
                    )
                )
            # The note's TEXT changed even though its body did not, so its chunks and
            # embeddings are as stale as after an edit — and the graph derived from
            # them with it. Same reset `update_note` does. ONCE for the whole set, which
            # is the point of taking pairs: a caller looping over the one-pair wrapper
            # would flip the state and enqueue an `ingest_note` per answer, and N
            # re-ingests of one note is exactly the cost the batched ask exists to remove.
            note.ingest_state = "pending"
            # `updated_at` is deliberately NOT stamped: the body is frozen (D6) and
            # nothing edited it. The append is a new row, not a revision.
            #
            # The re-ingest is enqueued HERE, in the same transaction, rather than left
            # to the caller. Every other write path enqueues from its own API handler
            # (api/notes.py:244,302,325) and `create_note` carries a standing FUTURE
            # note that a new path can silently forget to — which is exactly how
            # proposal-enacted notes never indexed. This path has no HTTP route to hang
            # the enqueue off (W3's `ask_owner` tool is the caller), so the choice was
            # "in the transaction" or "nowhere reliable". In the transaction also means
            # a rolled-back append queues no work, and a committed one cannot fail to.
            await enqueue_on(session, "ingest_note", {"note_id": str(note.id)})
            await session.flush()
            await session.refresh(note)
            return _note_info(note)

    async def list_clarifications(
        self, ctx: SessionContext, note_id: str
    ) -> list[ClarificationInfo] | None:
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(
                    select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if note is None:
                return None
            return [
                ClarificationInfo(
                    id=str(c.id),
                    seq=c.seq,
                    question=c.question,
                    answer=c.answer,
                    created_at=c.created_at,
                )
                for c in note.clarifications
            ]

    async def delete_clarification(
        self, ctx: SessionContext, note_id: str, clarification_id: str
    ) -> NoteInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(
                    select(Note).where(Note.id == note_id, Note.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if note is None:
                return None
            removed = (
                await session.execute(
                    delete(NoteClarification)
                    .where(
                        NoteClarification.id == clarification_id,
                        # The note predicate as well as the id: an id from ANOTHER note
                        # must not be deletable through this note's route, and RLS only
                        # narrows by domain.
                        NoteClarification.note_id == note.id,
                    )
                    .returning(NoteClarification.id)
                )
            ).scalar_one_or_none()
            if removed is None:
                return None
            # The note's text shrank, so its chunks, embeddings and the graph derived
            # from them are stale — the same reset and the same in-transaction enqueue
            # the append does. A redaction whose old chunk stayed in the search index
            # would not be one.
            note.ingest_state = "pending"
            await enqueue_on(session, "ingest_note", {"note_id": str(note.id)})
            await session.flush()
            await session.refresh(note)
            return _note_info(note)

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
                    words=r.words,
                )
                for r in rows
            ]
