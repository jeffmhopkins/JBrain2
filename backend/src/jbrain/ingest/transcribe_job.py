"""The transcribe_attachment job handler: one audio attachment -> a transcript
cache row.

The audio sibling of jbrain.ingest.ocr. Same doctrine (docs/ANALYSIS.md):
capture-to-searchable never waits on the model, so transcription is an async job,
never inline in ingest_note — which only reads the attachment_extracts cache
(ingest.extract.image_segments, kind-agnostic). The handler writes one
kind='transcript' row (delete + insert, the chunks pattern, so a retry is
idempotent), then re-enqueues ingest_note so the rebuilt chunks pick it up.

Load-on-demand / unload-after: the model lives in the llama-swap gateway, which
loads it on first request; once the transcription returns, the handler asks the
gateway to unload it so VRAM is freed immediately rather than at the idle timeout.
That eviction is best-effort — an optimization, never correctness — so a gateway
that can't be reached for it is logged and ignored, exactly like the settings
screen tolerates a down gateway (jbrain.llm.local_gateway).

Confidence is honest and capped ("Guards", docs/ANALYSIS.md): a transcription is
machine-heard, not author-written, so facts later mined from it inherit reduced
confidence. It reads cleaner than OCR (audio carries no layout ambiguity) but is
still a model's hearing, so it sits just below note text. An empty transcript
(silence / non-speech audio) keeps a confidence-0 row — the cache marker that
stops re-ingest from re-enqueueing the job.

Transient faults propagate and ride the queue's retry backoff; nothing is written
until the call succeeds, so a failed run never half-fills the cache.
"""

from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.models.notes import Attachment, AttachmentExtract, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore
from jbrain.transcribe import TranscribeClient
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

# In-code only (NOT an app.actions seed row, so migration 0035's seed-lockstep is
# untouched) — the sibling of ocr_attachment, enqueued directly by ingest, never
# referenced by a seeded pipeline. The worker adds it to its build_registry tuple
# like the other post-Phase-4 actions (eval_run, skill_*, wiki_*).
TRANSCRIBE_ATTACHMENT_SPEC = ActionSpec(
    name="transcribe_attachment",
    version=1,
    handler="transcribe_attachment",
    domain_optional=True,
    mutating=True,
    cost_class="expensive",
    dedup_key_expr="attachment_id",
    description="Transcribe an audio attachment with the local whisper model.",
)

# A transcription is a model's hearing, not the author's words: capped below note
# text (1.0), above OCR (0.7) — audio has no layout to misread, but it is still
# machine-produced and a low-confidence value must never auto-supersede a fact.
TRANSCRIPT_CONFIDENCE = 0.8

KIND_TRANSCRIPT = "transcript"

# The per-attachment size budget's default (config.whisper_max_bytes overrides it);
# the MAX_OCR_BYTES sibling. Ingest skips enqueueing transcription for larger files
# — no cache row, so a smaller re-upload transcribes normally.
DEFAULT_TRANSCRIBE_MAX_BYTES = 100 * 1024 * 1024


class TranscribePipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        client: TranscribeClient,
        model: str,
        *,
        gateway: LocalGateway | None = None,
    ):
        self._maker = maker
        self._blobs = blobs
        self._client = client
        self._model = model
        self._gateway = gateway

    async def transcribe_attachment(self, payload: dict[str, Any]) -> None:
        """Handle a transcribe_attachment job: {attachment_id}; gone rows no-op."""
        attachment_id = str(payload["attachment_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            att = (
                await session.execute(select(Attachment).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            if att is None:
                log.info("transcribe.skipped", attachment_id=attachment_id, reason="gone")
                return
            note = (
                await session.execute(select(Note).where(Note.id == att.note_id))
            ).scalar_one_or_none()
            if note is None or note.deleted_at is not None:
                log.info("transcribe.skipped", attachment_id=attachment_id, reason="note gone")
                return
            note_id = str(att.note_id)
            sha256, media_type, filename, domain = (
                att.sha256,
                att.media_type,
                att.filename,
                att.domain_code,
            )
            # Transcription re-runs only when its cache row is missing: a re-ingest
            # of an already-transcribed note must not re-bill the model.
            has_transcript = (
                await session.execute(
                    select(AttachmentExtract.id)
                    .where(
                        AttachmentExtract.attachment_id == attachment_id,
                        AttachmentExtract.kind == KIND_TRANSCRIPT,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none() is not None
            attachment_uuid = att.id

        if has_transcript:
            log.info("transcribe.skipped", attachment_id=attachment_id, reason="already cached")
            return

        data = await self._blobs.get(sha256)
        try:
            transcript = await self._client.transcribe(
                data, filename=filename, media_type=media_type
            )
        finally:
            # Free the model whether the call succeeded or raised: a failed
            # transcription rides the retry backoff, and a model left resident
            # between widely-spaced retries wastes VRAM the local LLM needs.
            await self._unload()

        clean = transcript.text.strip()
        row = AttachmentExtract(
            attachment_id=attachment_uuid,
            kind=KIND_TRANSCRIPT,
            tool=f"whisper:{self._model}",
            text=clean,
            confidence=TRANSCRIPT_CONFIDENCE if clean else 0.0,
            source_anchor=filename,
            domain_code=domain,
        )
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await session.execute(
                delete(AttachmentExtract).where(
                    AttachmentExtract.attachment_id == attachment_id,
                    AttachmentExtract.kind == KIND_TRANSCRIPT,
                )
            )
            session.add(row)
        # Rebuild chunks so the transcript becomes searchable and the analysis that
        # follows ingest sees it.
        await queue.enqueue(self._maker, SYSTEM_CTX, "ingest_note", {"note_id": note_id})
        log.info(
            "transcribe.extracted",
            attachment_id=attachment_id,
            note_id=note_id,
            chars=len(clean),
            language=transcript.language,
        )

    async def _unload(self) -> None:
        """Best-effort eviction of the model from the gateway (load-on-demand /
        unload-after). Never raises: freeing VRAM is an optimization, and the
        gateway TTL-unloads anyway if this can't reach it."""
        if self._gateway is None:
            return
        try:
            await self._gateway.unload(self._model)
        except LocalGatewayError as exc:
            log.info("transcribe.unload_failed", model=self._model, error=str(exc))
