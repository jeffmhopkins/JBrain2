"""The ingest_note job handler: note + attachments -> searchable chunks.

Idempotent by design: each run deletes the note's existing chunks and rebuilds
them in one transaction, so re-enqueueing (new attachment, retry, backfill) is
always safe. Chunk ids are therefore not stable across re-ingestion — fine
until Phase 3 starts hanging facts off chunks.

Runs under the owner-kind system context (queue.SYSTEM_CTX): ingestion is the
owner's own machinery and must read notes in every domain; RLS still applies,
it simply resolves to full access for the owner kind.
"""

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.ingest.chunker import chunk_text
from jbrain.ingest.extract import ExtractorRegistry, default_registry
from jbrain.models.notes import Chunk, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore

log = structlog.get_logger()


@dataclass(frozen=True)
class _AttachmentRef:
    id: UUID
    media_type: str
    sha256: str


class IngestPipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        registry: ExtractorRegistry | None = None,
    ):
        self._maker = maker
        self._blobs = blobs
        self._registry = registry or default_registry()

    async def ingest_note(self, payload: dict[str, Any]) -> None:
        """Handle an ingest_note job: {note_id}; missing note is a no-op."""
        note_id = str(payload["note_id"])

        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            note = (
                await session.execute(select(Note).where(Note.id == note_id))
            ).scalar_one_or_none()
            if note is None or note.deleted_at is not None:
                log.info("ingest.skipped", note_id=note_id, reason="missing or deleted")
                return
            note.ingest_state = "processing"
            body = note.body
            domain = note.domain_code
            attachments = [
                _AttachmentRef(id=a.id, media_type=a.media_type, sha256=a.sha256)
                for a in note.attachments
            ]

        try:
            chunks = await self._build_chunks(note_id, domain, body, attachments)
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                await session.execute(delete(Chunk).where(Chunk.note_id == note_id))
                session.add_all(chunks)
                await session.execute(
                    update(Note)
                    .where(Note.id == note_id)
                    .values(ingest_state="indexed", indexed_at=func.now())
                )
        except Exception:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                await session.execute(
                    update(Note).where(Note.id == note_id).values(ingest_state="failed")
                )
            raise
        # Embedding is a follow-up job, not part of ingest: 'indexed' keeps
        # meaning chunked + FTS-ready, and a dead embed container can't block
        # keyword search. Re-ingest re-enqueues because the rebuilt chunks
        # all start with NULL embeddings.
        await queue.enqueue(self._maker, SYSTEM_CTX, "embed_note", {"note_id": note_id})
        log.info("ingest.indexed", note_id=note_id, chunks=len(chunks))

    async def _build_chunks(
        self, note_id: str, domain: str, body: str, attachments: list[_AttachmentRef]
    ) -> list[Chunk]:
        seq = 0
        chunks: list[Chunk] = []

        def add(
            text_chunks: Any, *, attachment_id: UUID | None, source_kind: str, anchor: str | None
        ) -> None:
            nonlocal seq
            for tc in text_chunks:
                chunks.append(
                    Chunk(
                        note_id=UUID(note_id),
                        attachment_id=attachment_id,
                        domain_code=domain,
                        granularity=tc.granularity,
                        seq=seq,
                        char_start=tc.char_start,
                        char_end=tc.char_end,
                        source_kind=source_kind,
                        source_anchor=anchor,
                        text=tc.text,
                    )
                )
                seq += 1

        add(chunk_text(body), attachment_id=None, source_kind="note", anchor=None)
        for att in attachments:
            if self._registry.extractor_for(att.media_type) is None:
                continue
            data = await self._blobs.get(att.sha256)
            # Extraction is CPU-bound (PDF parsing); keep it off the event loop.
            segments = await asyncio.to_thread(self._registry.extract, att.media_type, data)
            for segment in segments:
                add(
                    chunk_text(segment.text),
                    attachment_id=att.id,
                    source_kind=segment.kind,
                    anchor=segment.anchor,
                )
        return chunks
