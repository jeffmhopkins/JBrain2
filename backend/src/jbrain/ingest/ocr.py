"""The ocr_attachment job handler: one image -> OCR + caption cache rows.

docs/ANALYSIS.md doctrine: capture-to-searchable never waits on a cloud LLM,
so vision work is an async job — never inline in ingest_note, which only
reads the attachment_extracts cache (ingest.extract.image_segments). The
handler makes exactly one vision.ocr call and one vision.caption call through
the adapter (OCR and captioning are separate products [decided]), rewrites
the attachment's cache rows (delete + insert — the chunks pattern, so retries
and tool-upgrade re-OCR are idempotent), then re-enqueues ingest_note so the
rebuilt chunks pick the cache up. Usage lands in llm_usage automatically: the
router records every provider call regardless of task.

Confidence is honest and capped ("Guards"): OCR output never claims more than
0.7, so facts later extracted from it inherit reduced confidence and a
low-confidence numeric health value can never auto-supersede anything (the
supersession decide() machinery keys on fact confidence). A caption is a
model's description, not a transcription, and sits lower still. An image with
no legible text keeps an empty-text row at confidence 0 — the row is the
cache marker that stops re-ingest from looping back into OCR.

Transient LLM faults propagate and ride the queue's retry backoff; nothing is
written until both calls succeed, so a failed run never half-fills the cache.
"""

import base64
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.db.session import scoped_session
from jbrain.llm import LlmImage, LlmRouter
from jbrain.models.notes import Attachment, AttachmentExtract, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore

log = structlog.get_logger()

# Per-task size budget (docs/ANALYSIS.md "Dispatcher-level policy"): ingest
# skips ENQUEUEING OCR for larger images, with a logged warning — no cache
# row, so shrinking the file and re-ingesting picks it up again.
MAX_OCR_BYTES = 8 * 1024 * 1024

# The Guards cap: OCR text is machine-read, not author-written.
OCR_CONFIDENCE = 0.7
# A caption describes; it does not transcribe.
CAPTION_CONFIDENCE = 0.6

# OCR of a dense page can outweigh the image tokens; captions are one sentence.
OCR_MAX_TOKENS = 8192
CAPTION_MAX_TOKENS = 256

OCR_SYSTEM = (
    "You transcribe text from one image. Transcribe ALL legible text "
    "verbatim, preserving the original line structure. Output plain text "
    "only — no commentary, no markdown, no code fences. Be honest about "
    "illegibility: write [illegible] where you cannot read a word or "
    "region, and never guess. If the image contains no legible text at "
    "all, output nothing."
)

CAPTION_SYSTEM = (
    "You caption one image for a personal knowledge index. Reply with "
    "exactly one plain-text sentence describing what the image shows — "
    "no preamble, no quotes, no markdown."
)


def build_extracts(
    *,
    attachment_id: Any,
    domain: str,
    filename: str,
    ocr_text: str,
    caption_text: str,
    ocr_tool: str,
    caption_tool: str,
) -> list[AttachmentExtract]:
    """The two cache rows one vision pass produces, confidence pre-capped."""
    rows = []
    for kind, text, tool, cap in (
        ("ocr", ocr_text.strip(), ocr_tool, OCR_CONFIDENCE),
        ("caption", caption_text.strip(), caption_tool, CAPTION_CONFIDENCE),
    ):
        rows.append(
            AttachmentExtract(
                attachment_id=attachment_id,
                kind=kind,
                tool=tool,
                text=text,
                confidence=cap if text else 0.0,
                source_anchor=filename,
                domain_code=domain,
            )
        )
    return rows


class OcrPipeline:
    def __init__(
        self, maker: async_sessionmaker[AsyncSession], blobs: BlobStore, router: LlmRouter
    ):
        self._maker = maker
        self._blobs = blobs
        self._router = router

    async def ocr_attachment(self, payload: dict[str, Any]) -> None:
        """Handle an ocr_attachment job: {attachment_id}; gone rows no-op."""
        attachment_id = str(payload["attachment_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            att = (
                await session.execute(select(Attachment).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            if att is None:
                log.info("ocr.skipped", attachment_id=attachment_id, reason="attachment gone")
                return
            note = (
                await session.execute(select(Note).where(Note.id == att.note_id))
            ).scalar_one_or_none()
            if note is None or note.deleted_at is not None:
                log.info("ocr.skipped", attachment_id=attachment_id, reason="note gone")
                return
            note_id = str(att.note_id)
            sha256, media_type, filename, domain = (
                att.sha256,
                att.media_type,
                att.filename,
                att.domain_code,
            )

        data = await self._blobs.get(sha256)
        image = LlmImage(media_type=media_type, data=base64.b64encode(data).decode("ascii"))
        ocr = await self._router.complete(
            "vision.ocr",
            system=OCR_SYSTEM,
            user_text=f"Transcribe this image (file: {filename}).",
            images=[image],
            max_tokens=OCR_MAX_TOKENS,
        )
        caption = await self._router.complete(
            "vision.caption",
            system=CAPTION_SYSTEM,
            user_text=f"Caption this image (file: {filename}).",
            images=[image],
            max_tokens=CAPTION_MAX_TOKENS,
        )

        rows = build_extracts(
            attachment_id=att.id,
            domain=domain,
            filename=filename,
            ocr_text=ocr.text,
            caption_text=caption.text,
            ocr_tool=":".join(self._router.spec("vision.ocr")),
            caption_tool=":".join(self._router.spec("vision.caption")),
        )
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await session.execute(
                delete(AttachmentExtract).where(AttachmentExtract.attachment_id == attachment_id)
            )
            session.add_all(rows)
        # Rebuild chunks so the new cache rows become searchable (and the
        # re-analysis that follows ingest sees the OCR text).
        await queue.enqueue(self._maker, SYSTEM_CTX, "ingest_note", {"note_id": note_id})
        log.info(
            "ocr.extracted",
            attachment_id=attachment_id,
            note_id=note_id,
            ocr_chars=len(rows[0].text),
            caption_chars=len(rows[1].text),
        )
