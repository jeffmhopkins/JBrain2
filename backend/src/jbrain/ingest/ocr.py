"""The ocr_attachment job handler: one image -> OCR (+ description) cache rows.

docs/ANALYSIS.md doctrine: capture-to-searchable never waits on a cloud LLM,
so vision work is an async job — never inline in ingest_note, which only
reads the attachment_extracts cache (ingest.extract.image_segments). What the
handler calls is the image-analysis mode [decided: default full]: "full"
makes one vision.ocr call and one vision.caption call (the caption kind now
carries a salient multi-sentence description the fact pipeline mines); "ocr"
makes the transcription call only and writes no caption row. The mode is read
from app.settings per job, and the payload's optional `mode` overrides it —
the on-demand analyze endpoint always sends "full", re-running the
description (delete + insert, the chunks pattern) and OCR only if missing.
The handler then re-enqueues ingest_note so the rebuilt chunks pick the cache
up. Usage lands in llm_usage automatically: the router records every
provider call regardless of task.

Confidence is honest and capped ("Guards"): OCR output never claims more than
0.7, so facts later extracted from it inherit reduced confidence and a
low-confidence numeric health value can never auto-supersede anything (the
supersession decide() machinery keys on fact confidence). A description is a
model's reading, not a transcription, and sits lower still. An image with no
legible text keeps an empty-text row at confidence 0 — the row is the cache
marker that stops re-ingest from looping back into OCR.

Transient LLM faults propagate and ride the queue's retry backoff; nothing is
written until every call succeeds, so a failed run never half-fills the cache.
"""

import base64
from typing import Any, Protocol

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import LlmImage, LlmRouter
from jbrain.models.notes import Attachment, AttachmentExtract, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import IMAGE_ANALYSIS_MODES
from jbrain.storage import BlobStore

log = structlog.get_logger()

# Per-task size budget (docs/ANALYSIS.md "Dispatcher-level policy"): ingest
# skips ENQUEUEING OCR for larger images, with a logged warning — no cache
# row, so shrinking the file and re-ingesting picks it up again.
MAX_OCR_BYTES = 8 * 1024 * 1024

# The Guards cap: OCR text is machine-read, not author-written.
OCR_CONFIDENCE = 0.7
# A description describes; it does not transcribe.
DESCRIPTION_CONFIDENCE = 0.6
EXTRACT_CONFIDENCE = {"ocr": OCR_CONFIDENCE, "caption": DESCRIPTION_CONFIDENCE}

# OCR of a dense page can outweigh the image tokens; descriptions are a few
# sentences.
OCR_MAX_TOKENS = 8192
DESCRIPTION_MAX_TOKENS = 512

OCR_SYSTEM = (
    "You transcribe text from one image. Transcribe ALL legible text "
    "verbatim, preserving the original line structure. Output plain text "
    "only — no commentary, no markdown, no code fences. Be honest about "
    "illegibility: write [illegible] where you cannot read a word or "
    "region, and never guess. If the image contains no legible text at "
    "all, output nothing."
)

DESCRIPTION_SYSTEM = (
    "You describe one image for a personal knowledge index. A separate "
    "pass transcribes its text — do not transcribe. Describe what the "
    "image shows that is worth knowing: objects, people, places, the "
    "context any visible text appears in, visible states or conditions, "
    "and the relationships you can see between them. Write 2-5 plain "
    "sentences. Describe only what is visible — never speculate beyond "
    "it. Output plain text only — no preamble, no markdown."
)


class ModeSource(Protocol):
    """The slice of the settings store the handler reads (faked in tests)."""

    async def image_analysis_mode(self, ctx: SessionContext) -> str: ...


def resolve_mode(requested: Any, configured: str) -> str:
    """Per-job mode: an explicit payload override (the on-demand analyze
    endpoint always sends "full") beats the stored setting; anything
    unrecognized falls back to the configured mode."""
    return requested if requested in IMAGE_ANALYSIS_MODES else configured


def build_extract(
    *, attachment_id: Any, domain: str, filename: str, kind: str, text: str, tool: str
) -> AttachmentExtract:
    """One cache row, confidence pre-capped per kind (zero when empty)."""
    clean = text.strip()
    return AttachmentExtract(
        attachment_id=attachment_id,
        kind=kind,
        tool=tool,
        text=clean,
        confidence=EXTRACT_CONFIDENCE[kind] if clean else 0.0,
        source_anchor=filename,
        domain_code=domain,
    )


class OcrPipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        router: LlmRouter,
        modes: ModeSource,
    ):
        self._maker = maker
        self._blobs = blobs
        self._router = router
        self._modes = modes

    async def ocr_attachment(self, payload: dict[str, Any]) -> None:
        """Handle an ocr_attachment job: {attachment_id, mode?}; gone rows no-op."""
        attachment_id = str(payload["attachment_id"])
        mode = resolve_mode(payload.get("mode"), await self._modes.image_analysis_mode(SYSTEM_CTX))
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
            # OCR is re-run only when its cache row is missing (the on-demand
            # path re-describes without re-billing a transcription).
            has_ocr = (
                await session.execute(
                    select(AttachmentExtract.id)
                    .where(
                        AttachmentExtract.attachment_id == attachment_id,
                        AttachmentExtract.kind == "ocr",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none() is not None

        run_kinds = [*([] if has_ocr else ["ocr"]), *(["caption"] if mode == "full" else [])]
        if not run_kinds:
            log.info("ocr.skipped", attachment_id=attachment_id, reason="nothing to run")
            return

        data = await self._blobs.get(sha256)
        image = LlmImage(media_type=media_type, data=base64.b64encode(data).decode("ascii"))
        rows: list[AttachmentExtract] = []
        if "ocr" in run_kinds:
            ocr = await self._router.complete(
                "vision.ocr",
                system=OCR_SYSTEM,
                user_text=f"Transcribe this image (file: {filename}).",
                images=[image],
                max_tokens=OCR_MAX_TOKENS,
            )
            rows.append(
                build_extract(
                    attachment_id=att.id,
                    domain=domain,
                    filename=filename,
                    kind="ocr",
                    text=ocr.text,
                    tool=":".join(self._router.spec("vision.ocr")),
                )
            )
        if "caption" in run_kinds:
            description = await self._router.complete(
                "vision.caption",
                system=DESCRIPTION_SYSTEM,
                user_text=f"Describe this image (file: {filename}).",
                images=[image],
                max_tokens=DESCRIPTION_MAX_TOKENS,
            )
            rows.append(
                build_extract(
                    attachment_id=att.id,
                    domain=domain,
                    filename=filename,
                    kind="caption",
                    text=description.text,
                    tool=":".join(self._router.spec("vision.caption")),
                )
            )

        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            # Delete + insert only the kinds this run recomputed: the chunks
            # pattern keeps retries idempotent, and an on-demand re-describe
            # must not drop a still-valid transcription.
            await session.execute(
                delete(AttachmentExtract).where(
                    AttachmentExtract.attachment_id == attachment_id,
                    AttachmentExtract.kind.in_(run_kinds),
                )
            )
            session.add_all(rows)
        # Rebuild chunks so the new cache rows become searchable (and the
        # re-analysis that follows ingest sees the OCR text).
        await queue.enqueue(self._maker, SYSTEM_CTX, "ingest_note", {"note_id": note_id})
        log.info(
            "ocr.extracted",
            attachment_id=attachment_id,
            note_id=note_id,
            mode=mode,
            kinds=run_kinds,
            chars={r.kind: len(r.text) for r in rows},
        )
