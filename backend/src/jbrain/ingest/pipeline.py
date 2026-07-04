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
from sqlalchemy import bindparam, case, delete, select, text, update
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
from jbrain.ingest.transcribe_job import DEFAULT_TRANSCRIBE_MAX_BYTES
from jbrain.models.notes import AttachmentExtract, Chunk, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore
from jbrain.workflow import events as wf_events

log = structlog.get_logger()

# Attachment media types the EMR-import triggers key on in the note.ingested payload
# (docs/plans/EMR_IMPORT_PLAN.md §6.0): an archive is the pre-decryption import marker,
# a decrypted PDF is the post-intake parse marker.
ZIP_MEDIA_TYPES = ("application/zip", "application/x-zip-compressed")
PDF_MEDIA_TYPE = "application/pdf"


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
        *,
        transcribe_enabled: bool = False,
        transcribe_max_bytes: int = DEFAULT_TRANSCRIBE_MAX_BYTES,
    ):
        self._maker = maker
        self._blobs = blobs
        self._registry = registry or default_registry()
        # Audio transcription is an opt-in backend (the whisper gateway): when it
        # is unconfigured, audio attachments enqueue no job and simply yield no
        # chunks — never a queued transcribe_attachment with no handler-reachable
        # model. The worker flips this on when settings.whisper_url is set.
        self._transcribe_enabled = transcribe_enabled
        self._transcribe_max_bytes = transcribe_max_bytes

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
            destination = note.destination
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
                    .values(
                        ingest_state="indexed",
                        indexed_at=func.now(),
                        # A re-ingest of an already-integrated note (an edit, or the
                        # OCR handler's re-describe) rebuilt the chunks, so the graph
                        # is now stale: flip 'integrated' -> 'stale' so it is eligible
                        # again under `integration_state <> 'integrated'` — both the
                        # engine dispatcher's _already_active skip and the integration
                        # reconciler key on that, and without the reset a re-delivered
                        # note.ingested for the SAME unchanged note would be skipped but
                        # so would this genuine re-integration. Pre-cutover the direct
                        # integrate enqueue re-ran unconditionally; this preserves that.
                        integration_state=case(
                            (Note.integration_state == "integrated", "stale"),
                            else_=Note.integration_state,
                        ),
                    )
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
        # Audio rides the identical doctrine: an uncached audio attachment gets an
        # async transcribe_attachment job whose handler re-ingests, so the cache
        # check is what keeps that loop from spinning. Its outstanding ids join the
        # OCR set — analysis waits on EITHER backend.
        outstanding |= await self._enqueue_transcribe_jobs(note_id, attachments, set(extracts))
        # Extraction is likewise a follow-up job (ingest stays LLM-free,
        # docs/reference/ANALYSIS.md), gated on outstanding vision WORK — never on
        # extract kinds or the image-analysis mode: a mode flip on a cached
        # attachment enqueues no job and must not block analysis. While OCR is
        # outstanding the OCR handler's re-ingest re-emits this event with its OCR
        # text, so an image note is extracted once. The queued-only dedup is now the
        # dispatcher's job (_already_active): a RUNNING analyze may have read stale
        # chunks, so it never suppresses a fresh pass; a queued twin does, and so does
        # an integrated note — but a re-ingest just flipped 'integrated' -> 'stale'
        # above, so this re-emission re-integrates and only a duplicate event for an
        # unchanged note is suppressed. Single-worker invariant: the OCR handler
        # re-ingests before its own job completes, which is safe single-threaded —
        # under multi-worker the gate could see that originating job as running and
        # defer forever. TODO(queue): triggered_by_job exclusion if multi-worker lands.
        if not outstanding and not await queue.has_active_analysis(
            self._maker, SYSTEM_CTX, note_id, statuses=("queued",)
        ):
            # W2·C cutover: emit the note.ingested event that DRIVES integration — the
            # dispatcher resolves it to the integrate pipeline and enqueues the job
            # (with the same queued-only / already-integrated dedup the direct enqueue
            # carried, now in dispatcher._already_active). The direct integrate enqueue
            # is gone; only the event remains. domain is the note's fail-closed E2
            # stamp; the worker has no per-content principal, so the emit resolves the
            # owner principal. Best-effort — a failed emit never disturbs ingestion;
            # the recurring integration reconciler (backfill_pending_integration) is the
            # safety net for a dropped event. The has_active_analysis gate above stays
            # as a cheap pre-filter (skip emitting when a queued integrate already
            # exists), but the dispatcher's dedup is the authoritative once-only.
            # The payload carries the pre-decryption markers the EMR-import triggers key
            # on (docs/plans/EMR_IMPORT_PLAN.md §6.0/§12.2 #4): `destination` +
            # whether the note currently holds an archive vs a decrypted PDF. These are
            # visible on the raw/ingested note, so the trigger stays a precise
            # user-chosen marker via `payload_equals` — never a body-text guess. Extra
            # keys are inert for every other note.ingested trigger (payload_equals is a
            # subset match; forward_keys still defaults to {note_id}).
            await wf_events.emit_event(
                self._maker,
                SYSTEM_CTX,
                type=wf_events.NOTE_INGESTED,
                domain_code=domain,
                payload={
                    "note_id": note_id,
                    "destination": destination,
                    "has_zip_attachment": any(a.media_type in ZIP_MEDIA_TYPES for a in attachments),
                    "has_pdf_attachment": any(a.media_type == PDF_MEDIA_TYPE for a in attachments),
                },
                enqueued=wf_events.shadow_enqueued("integrate_note", {"note_id": note_id}),
            )
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
        budget, docs/reference/ANALYSIS.md "Dispatcher-level policy") — deliberately
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

    async def _enqueue_transcribe_jobs(
        self, note_id: str, attachments: list[_AttachmentRef], cached: set[UUID]
    ) -> set[str]:
        """The audio twin of _enqueue_ocr_jobs: one transcribe_attachment job per
        audio attachment with no cache rows yet; returns the attachment ids with
        transcription outstanding after this run (newly enqueued + already
        queued/running). No-op when the whisper backend is unconfigured — audio
        then yields no chunks rather than a job with no reachable model.

        Oversized files are skipped at enqueue time (the per-task budget,
        docs/reference/ANALYSIS.md), deliberately without a cache row, so a smaller
        re-upload transcribes normally; they are never outstanding, so they never
        block analysis.
        """
        if not self._transcribe_enabled:
            return set()
        audio_ids = [str(a.id) for a in attachments if a.media_type.startswith("audio/")]
        if not audio_ids:
            return set()
        candidates: list[_AttachmentRef] = []
        for att in attachments:
            if not att.media_type.startswith("audio/") or att.id in cached:
                continue
            if att.size_bytes > self._transcribe_max_bytes:
                log.warning(
                    "ingest.transcribe_skipped_too_large",
                    attachment_id=str(att.id),
                    size_bytes=att.size_bytes,
                    cap_bytes=self._transcribe_max_bytes,
                )
                continue
            candidates.append(att)
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            outstanding = set(
                (
                    await session.execute(
                        text(
                            "SELECT payload->>'attachment_id' FROM app.jobs"
                            " WHERE kind = 'transcribe_attachment'"
                            " AND status IN ('queued', 'running')"
                            " AND payload->>'attachment_id' IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": audio_ids},
                    )
                ).scalars()
            )
        for att in candidates:
            if str(att.id) in outstanding:
                continue
            await queue.enqueue(
                self._maker, SYSTEM_CTX, "transcribe_attachment", {"attachment_id": str(att.id)}
            )
            outstanding.add(str(att.id))
            log.info("ingest.transcribe_enqueued", note_id=note_id, attachment_id=str(att.id))
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
                # (docs/reference/ANALYSIS.md "Attachments") — OCR/captioning already
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
