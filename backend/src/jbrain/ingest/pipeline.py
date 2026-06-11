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
from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.ingest.chunker import chunk_text
from jbrain.ingest.extract import (
    CachedExtract,
    ExtractorRegistry,
    default_registry,
    image_segments,
)
from jbrain.ingest.ocr import MAX_OCR_BYTES
from jbrain.models.notes import AttachmentExtract, Chunk, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore

log = structlog.get_logger()


@dataclass(frozen=True)
class _AttachmentRef:
    id: UUID
    media_type: str
    sha256: str
    filename: str
    size_bytes: int


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
                _AttachmentRef(
                    id=a.id,
                    media_type=a.media_type,
                    sha256=a.sha256,
                    filename=a.filename,
                    size_bytes=a.size_bytes,
                )
                for a in note.attachments
            ]
            extracts = await self._load_extracts(session, attachments)

        try:
            chunks = await self._build_chunks(note_id, domain, body, attachments, extracts)
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
        # Vision OCR rides the same doctrine: images whose cache is empty get
        # an async ocr_attachment job; the handler re-enqueues ingest_note, so
        # the cache check is what keeps that loop from spinning.
        outstanding = await self._enqueue_ocr_jobs(note_id, attachments, set(extracts))
        # Extraction is likewise a follow-up job (ingest stays LLM-free,
        # docs/ANALYSIS.md), gated on outstanding vision WORK — never on
        # extract kinds or the image-analysis mode: a mode flip on a cached
        # attachment enqueues no job and must not block analysis. While OCR is
        # outstanding the handler's re-ingest enqueues analysis instead, so an
        # image note is extracted once, with its OCR text. The dedup is
        # queued-only on purpose: a RUNNING analyze may have read stale
        # chunks, so a fresh pass must still follow it. Single-worker
        # invariant: the OCR handler enqueues this re-ingest before its own
        # job completes, which is safe single-threaded — under multi-worker
        # the gate could see that originating job as running and defer
        # forever. TODO(queue): triggered_by_job exclusion if multi-worker
        # lands.
        if not outstanding and not await queue.has_active(
            self._maker,
            SYSTEM_CTX,
            "analyze_note",
            payload_field="note_id",
            value=note_id,
            statuses=("queued",),
        ):
            await queue.enqueue(self._maker, SYSTEM_CTX, "analyze_note", {"note_id": note_id})
        log.info("ingest.indexed", note_id=note_id, chunks=len(chunks))

    async def _load_extracts(
        self, session: AsyncSession, attachments: list[_AttachmentRef]
    ) -> dict[UUID, list[CachedExtract]]:
        """The vision-extract cache for these attachments, keyed by id."""
        if not attachments:
            return {}
        rows = (
            await session.execute(
                select(AttachmentExtract).where(
                    AttachmentExtract.attachment_id.in_([a.id for a in attachments])
                )
            )
        ).scalars()
        extracts: dict[UUID, list[CachedExtract]] = {}
        for row in rows:
            extracts.setdefault(row.attachment_id, []).append(
                CachedExtract(
                    kind=row.kind,
                    text=row.text,
                    anchor=row.source_anchor,
                    confidence=row.confidence if row.confidence is not None else 0.0,
                )
            )
        return extracts

    async def _enqueue_ocr_jobs(
        self, note_id: str, attachments: list[_AttachmentRef], cached: set[UUID]
    ) -> set[str]:
        """One ocr_attachment job per image with no cache rows yet; returns
        the attachment ids with OCR work outstanding after this run (newly
        enqueued + already queued/running) — the analysis gate's input.

        Oversized images are skipped at enqueue time (the per-task size
        budget, docs/ANALYSIS.md "Dispatcher-level policy") — deliberately
        without a cache row, so a re-uploaded smaller file OCRs normally;
        they are never outstanding, so they never block analysis. An already
        queued/running job suppresses duplicates the same way the startup
        backfills do. The active check spans ALL the note's images, not just
        cache-less candidates: an in-flight on-demand re-describe of a cached
        attachment is outstanding work too.
        """
        image_ids = [str(a.id) for a in attachments if a.media_type.startswith("image/")]
        candidates: list[_AttachmentRef] = []
        for att in attachments:
            if not att.media_type.startswith("image/") or att.id in cached:
                continue
            if att.size_bytes > MAX_OCR_BYTES:
                log.warning(
                    "ingest.ocr_skipped_too_large",
                    attachment_id=str(att.id),
                    size_bytes=att.size_bytes,
                    cap_bytes=MAX_OCR_BYTES,
                )
                continue
            candidates.append(att)
        if not image_ids:
            return set()
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            outstanding = set(
                (
                    await session.execute(
                        text(
                            "SELECT payload->>'attachment_id' FROM app.jobs"
                            " WHERE kind = 'ocr_attachment'"
                            " AND status IN ('queued', 'running')"
                            " AND payload->>'attachment_id' IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": image_ids},
                    )
                ).scalars()
            )
        for att in candidates:
            if str(att.id) in outstanding:
                continue
            await queue.enqueue(
                self._maker, SYSTEM_CTX, "ocr_attachment", {"attachment_id": str(att.id)}
            )
            outstanding.add(str(att.id))
            log.info("ingest.ocr_enqueued", note_id=note_id, attachment_id=str(att.id))
        return outstanding

    async def _build_chunks(
        self,
        note_id: str,
        domain: str,
        body: str,
        attachments: list[_AttachmentRef],
        extracts: dict[UUID, list[CachedExtract]],
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
            if att.id in extracts:
                # The image chain: a pure read over the vision-extract cache
                # (docs/ANALYSIS.md "Attachments") — OCR/captioning already
                # ran in the ocr_attachment job, never here.
                segments = image_segments(extracts[att.id])
            elif self._registry.extractor_for(att.media_type) is not None:
                data = await self._blobs.get(att.sha256)
                # Extraction is CPU-bound (PDF parsing); keep it off the
                # event loop.
                segments = await asyncio.to_thread(self._registry.extract, att.media_type, data)
            else:
                continue
            for segment in segments:
                add(
                    chunk_text(segment.text),
                    attachment_id=att.id,
                    source_kind=segment.kind,
                    anchor=segment.anchor or att.filename,
                )
        return chunks
