"""The ocr_attachment job handler: one image -> OCR (+ description) cache rows.

docs/reference/ANALYSIS.md doctrine: capture-to-searchable never waits on a cloud LLM,
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

import asyncio
import base64
import difflib
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import queue
from jbrain.analysis import flow_trace
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.imageprep import downscale_for_vision, pdf_page_images
from jbrain.llm import LlmImage, LlmRouter
from jbrain.llm.promptfile import load_prompt
from jbrain.models.notes import Attachment, AttachmentExtract, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import IMAGE_ANALYSIS_MODES
from jbrain.storage import BlobStore
from jbrain.vision import OcrResult, OcrServiceError, RapidOcrClient

log = structlog.get_logger()

# Per-task size budget (docs/reference/ANALYSIS.md "Dispatcher-level policy"): ingest
# skips ENQUEUEING OCR for larger images, with a logged warning — no cache
# row, so shrinking the file and re-ingesting picks it up again.
MAX_OCR_BYTES = 8 * 1024 * 1024
PDF_MEDIA_TYPE = "application/pdf"

# The Guards cap: OCR text is machine-read, not author-written.
OCR_CONFIDENCE = 0.7
# A description describes; it does not transcribe.
DESCRIPTION_CONFIDENCE = 0.6
EXTRACT_CONFIDENCE = {"ocr": OCR_CONFIDENCE, "caption": DESCRIPTION_CONFIDENCE}

# The deterministic RapidOCR engine's `tool` tag. Its transcription is stored as a SECOND
# `kind="ocr"` row (same 0.7 health-safety confidence cap as the VLM row — the cross-check
# raises trust in the string, never a fact's auto-supersede power); the chunker prefers it
# per anchor (jbrain.ingest.extract.image_segments). docs/plans/RAPIDOCR_PLAN.md.
RAPIDOCR_TOOL = "rapidocr"


def _agreement(vlm_text: str, rapid_text: str) -> float:
    """Normalized similarity of the two OCR readings (whitespace-collapsed, case-folded),
    0..1 — the divergence signal logged for the dual-engine cross-check."""
    a = " ".join(vlm_text.split()).lower()
    b = " ".join(rapid_text.split()).lower()
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# The vision prompts (system text, token budget, capability tier) are each one
# co-located .prompt artifact (docs/reference/DEVELOPMENT.md); these constants are the
# loader facade so the handler and tests keep importing the same names. Both run
# on the "vision" tier — the adapter resolves it to an image-capable model.
_OCR = load_prompt(Path(__file__).parent / "prompts" / "vision_ocr.prompt")
_DESCRIPTION = load_prompt(Path(__file__).parent / "prompts" / "vision_caption.prompt")
OCR_MAX_TOKENS = int(_OCR.config["max_tokens"])
DESCRIPTION_MAX_TOKENS = int(_DESCRIPTION.config["max_tokens"])
OCR_STRENGTH = _OCR.strength
DESCRIPTION_STRENGTH = _DESCRIPTION.strength
OCR_SYSTEM = _OCR.render()
DESCRIPTION_SYSTEM = _DESCRIPTION.render()


async def enqueue_analysis_fallback(
    maker: async_sessionmaker[AsyncSession], attachment_id: str
) -> str | None:
    """Attachment-job retry exhaustion fallback: analyze the note body-only.

    Shared by the exhausted ocr_attachment and transcribe_attachment paths (both
    feed the same analysis gate). The failed job row stays the durable record;
    analysis must not wait on text that will never arrive. Enqueues integrate_note
    DIRECTLY — a re-ingest would re-enqueue the still-cache-less attachment and
    loop. Skipped while another ocr_attachment OR transcribe_attachment job for the
    note is still active (that job's own completion or exhaustion triggers
    analysis) or an integrate_note job is already queued/running. Returns the job
    id, or None when nothing was enqueued.
    """
    async with scoped_session(maker, SYSTEM_CTX) as session:
        note_id = (
            await session.execute(select(Attachment.note_id).where(Attachment.id == attachment_id))
        ).scalar_one_or_none()
    if note_id is None:
        return None
    nid = str(note_id)
    if await queue.has_active_ocr_for_note(maker, SYSTEM_CTX, nid):
        return None
    if await queue.has_active_transcribe_for_note(maker, SYSTEM_CTX, nid):
        return None
    if await queue.has_active_analysis(maker, SYSTEM_CTX, nid):
        return None
    job_id = await queue.enqueue(maker, SYSTEM_CTX, "integrate_note", {"note_id": nid})
    log.warning("ocr.analysis_fallback", attachment_id=attachment_id, note_id=nid, job_id=job_id)
    return job_id


class ModeSource(Protocol):
    """The slice of the settings store the handler reads (faked in tests)."""

    async def image_analysis_mode(self, ctx: SessionContext) -> str: ...


def resolve_mode(requested: Any, configured: str) -> str:
    """Per-job mode: an explicit payload override (the on-demand analyze
    endpoint always sends "full") beats the stored setting; anything
    unrecognized falls back to the configured mode."""
    return requested if requested in IMAGE_ANALYSIS_MODES else configured


def build_extract(
    *,
    attachment_id: Any,
    domain: str,
    filename: str,
    kind: str,
    text: str,
    tool: str,
    anchor: str | None = None,
) -> AttachmentExtract:
    """One cache row, confidence pre-capped per kind (zero when empty). `anchor` sets
    `source_anchor` (a per-page `page N` for a scanned PDF); defaults to the filename
    for a single-image extract."""
    clean = text.strip()
    return AttachmentExtract(
        attachment_id=attachment_id,
        kind=kind,
        tool=tool,
        text=clean,
        confidence=EXTRACT_CONFIDENCE[kind] if clean else 0.0,
        source_anchor=anchor or filename,
        domain_code=domain,
    )


async def ocr_pdf_pages(router: LlmRouter, data: bytes, filename: str) -> list[str]:
    """Transcribe each page of a scanned (text-less) PDF via the `vision.ocr` route,
    in order — the shared page-OCR core for the ingest OCR job and the EMR importer.
    Each page is rasterized then downscaled under the vision token cap. An unreadable
    PDF yields no pages (rasterization degrades to empty, never raising)."""
    images = await asyncio.to_thread(pdf_page_images, data)
    texts: list[str] = []
    for number, png in enumerate(images, start=1):
        prepared, media_type = downscale_for_vision(png, "image/png")
        image = LlmImage(media_type=media_type, data=base64.b64encode(prepared).decode("ascii"))
        result = await router.complete(
            "vision.ocr",
            system=OCR_SYSTEM,
            user_text=f"Transcribe this scanned page ({filename}, page {number}).",
            images=[image],
            max_tokens=OCR_MAX_TOKENS,
            strength=OCR_STRENGTH,
        )
        texts.append(result.text.strip())
    return texts


class OcrPipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        router: LlmRouter,
        modes: ModeSource,
        rapidocr: RapidOcrClient | None = None,
    ):
        self._maker = maker
        self._blobs = blobs
        self._router = router
        self._modes = modes
        # The deterministic OCR cross-check (docs/plans/RAPIDOCR_PLAN.md). None ⇒ VLM-only
        # (the sidecar isn't configured); a per-call failure also degrades to VLM-only —
        # the cross-check never fails the OCR job.
        self._rapidocr = rapidocr

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

        # A scanned PDF is transcribed page by page (source_anchor="page N"), no
        # caption — captioning each page of a document is noise, not a reading.
        is_pdf = media_type == PDF_MEDIA_TYPE
        run_kinds = [
            *([] if has_ocr else ["ocr"]),
            *(["caption"] if mode == "full" and not is_pdf else []),
        ]
        if not run_kinds:
            log.info("ocr.skipped", attachment_id=attachment_id, reason="nothing to run")
            return

        data = await self._blobs.get(sha256)
        rows: list[AttachmentExtract] = []
        if is_pdf:
            if "ocr" in run_kinds:
                rows = await self._ocr_pdf_rows(att.id, data, filename, domain, note_id)
            await self._persist_extracts(attachment_id, note_id, mode, run_kinds, rows)
            return
        # Downscale oversized images so the vision model isn't handed thousands of
        # image tokens (slow + context overflow — the OCR timeout/retry loop).
        data, media_type = downscale_for_vision(data, media_type)
        image = LlmImage(media_type=media_type, data=base64.b64encode(data).decode("ascii"))
        if "ocr" in run_kinds:

            async def _vlm_ocr() -> str:
                result = await self._router.complete(
                    "vision.ocr",
                    system=OCR_SYSTEM,
                    user_text=f"Transcribe this image (file: {filename}).",
                    images=[image],
                    max_tokens=OCR_MAX_TOKENS,
                    strength=OCR_STRENGTH,
                )
                return result.text

            # The VLM reading and the deterministic RapidOCR transcription run concurrently;
            # RapidOCR degrades to None (VLM-only) when the sidecar is off/unreachable.
            vlm_text, rapid = await asyncio.gather(_vlm_ocr(), self._rapid_ocr(data, media_type))
            spec = await self._router.effective_spec("vision.ocr", OCR_STRENGTH)
            flow_trace.vision(
                attachment_id,
                note_id=note_id,
                kind="ocr",
                provider=spec[0],
                model=spec[1],
                filename=filename,
                text=vlm_text,
            )
            rows.append(
                build_extract(
                    attachment_id=att.id,
                    domain=domain,
                    filename=filename,
                    kind="ocr",
                    text=vlm_text,
                    tool=":".join(spec),
                )
            )
            if rapid is not None:
                self._log_crosscheck(attachment_id, note_id, filename, vlm_text, rapid.text)
                if rapid.text.strip():
                    rows.append(
                        self._rapid_row(att.id, domain, filename, note_id, rapid.text, anchor=None)
                    )
        if "caption" in run_kinds:
            description = await self._router.complete(
                "vision.caption",
                system=DESCRIPTION_SYSTEM,
                user_text=f"Describe this image (file: {filename}).",
                images=[image],
                max_tokens=DESCRIPTION_MAX_TOKENS,
                strength=DESCRIPTION_STRENGTH,
            )
            spec = await self._router.effective_spec("vision.caption", DESCRIPTION_STRENGTH)
            flow_trace.vision(
                attachment_id,
                note_id=note_id,
                kind="caption",
                provider=spec[0],
                model=spec[1],
                filename=filename,
                text=description.text,
            )
            rows.append(
                build_extract(
                    attachment_id=att.id,
                    domain=domain,
                    filename=filename,
                    kind="caption",
                    text=description.text,
                    tool=":".join(spec),
                )
            )

        await self._persist_extracts(attachment_id, note_id, mode, run_kinds, rows)

    async def _ocr_pdf_rows(
        self, att_id: Any, data: bytes, filename: str, domain: str, note_id: str
    ) -> list[AttachmentExtract]:
        """One `ocr` extract per scanned-PDF page (`source_anchor="page N"`) so the
        transcript chunks and cites per page (§6.2), not as one filename-anchored blob."""
        texts = await ocr_pdf_pages(self._router, data, filename)
        spec = await self._router.effective_spec("vision.ocr", OCR_STRENGTH)
        rows: list[AttachmentExtract] = []
        for number, text in enumerate(texts, start=1):
            flow_trace.vision(
                str(att_id),
                note_id=note_id,
                kind="ocr",
                provider=spec[0],
                model=spec[1],
                filename=f"{filename}#page-{number}",
                text=text,
            )
            rows.append(
                build_extract(
                    attachment_id=att_id,
                    domain=domain,
                    filename=filename,
                    kind="ocr",
                    text=text,
                    tool=":".join(spec),
                    anchor=f"page {number}",
                )
            )
        # Deterministic per-page cross-check (skipped when the sidecar is off).
        for number, rtext in enumerate(await self._rapid_pdf_pages(data), start=1):
            if not rtext.strip():
                continue
            if number <= len(texts):
                self._log_crosscheck(
                    str(att_id), note_id, f"page {number}", texts[number - 1], rtext
                )
            rows.append(
                self._rapid_row(att_id, domain, filename, note_id, rtext, anchor=f"page {number}")
            )
        return rows

    async def _rapid_ocr(self, data: bytes, media_type: str) -> OcrResult | None:
        """The deterministic cross-check for one image, or None when the sidecar is off or the
        call fails — the cross-check never fails the OCR job."""
        if self._rapidocr is None:
            return None
        try:
            return await self._rapidocr.ocr(data, media_type)
        except OcrServiceError as exc:
            log.warning("ocr.rapidocr_unavailable", error=str(exc))
            return None

    async def _rapid_pdf_pages(self, data: bytes) -> list[str]:
        """RapidOCR each rasterized PDF page ([] when the sidecar is off). A per-page failure
        yields "" for that page (the VLM row stands)."""
        if self._rapidocr is None:
            return []
        try:
            images = await asyncio.to_thread(pdf_page_images, data)
        except Exception:  # noqa: BLE001 - a rasterization hiccup degrades to VLM-only
            return []
        texts: list[str] = []
        for png in images:
            result = await self._rapid_ocr(png, "image/png")
            texts.append(result.text if result is not None else "")
        return texts

    def _rapid_row(
        self, att_id: Any, domain: str, filename: str, note_id: str, text: str, anchor: str | None
    ) -> AttachmentExtract:
        """The deterministic transcription as a `kind="ocr"` row tagged `tool="rapidocr"` —
        what the chunker prefers, at the same 0.7 health cap as the VLM row."""
        flow_trace.vision(
            str(att_id),
            note_id=note_id,
            kind="ocr",
            provider=RAPIDOCR_TOOL,
            model=RAPIDOCR_TOOL,
            filename=filename if anchor is None else f"{filename}#{anchor}",
            text=text,
        )
        return build_extract(
            attachment_id=att_id,
            domain=domain,
            filename=filename,
            kind="ocr",
            text=text,
            tool=RAPIDOCR_TOOL,
            anchor=anchor,
        )

    def _log_crosscheck(
        self, attachment_id: str, note_id: str, anchor: str, vlm_text: str, rapid_text: str
    ) -> None:
        log.info(
            "ocr.crosscheck",
            attachment_id=attachment_id,
            note_id=note_id,
            anchor=anchor,
            agreement=round(_agreement(vlm_text, rapid_text), 3),
            vlm_chars=len(vlm_text),
            rapid_chars=len(rapid_text),
        )

    async def _persist_extracts(
        self, attachment_id: str, note_id: str, mode: str, run_kinds: list[str], rows: list
    ) -> None:
        """Delete + insert only the kinds this run recomputed (the chunks pattern keeps
        retries idempotent), then re-ingest so the rebuilt chunks pick up the cache."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
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
