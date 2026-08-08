"""The jerv `ocr` tool: a direct, deterministic text read of an attached image or PDF via
the on-box RapidOCR sidecar (docs/plans/RAPIDOCR_PLAN.md).

The verbatim, hallucination-free counterpart to `analyze_image`: where `analyze_image` asks
a vision MODEL what an image says (a reading that can drift), `ocr` returns the exact text
RapidOCR transcribes — no model, no interpretation, no model swap. jerv resolves a chat
attachment by id under the session's RLS scope (a foreign/out-of-scope id reads as a clean
miss, never a leak), fetches its bytes, and OCRs it (a PDF page by page). It reads no owner
knowledge-base data and surfaces no card — jerv reports the text in its answer.
"""

import asyncio
import uuid

import structlog

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.ingest.imageprep import pdf_page_images
from jbrain.storage import BlobStore
from jbrain.vision import OcrServiceError, RapidOcrClient

log = structlog.get_logger()

_PDF_MEDIA_TYPE = "application/pdf"
_NO_IMAGE = "No attached image or PDF with that id is in this chat."
_NOT_SUPPORTED = "ocr reads images and PDFs — that attachment is neither."
_UNAVAILABLE = "The on-box OCR service isn't available right now."
_NO_TEXT = "No legible text was found in that file."


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def build_ocr_handlers(
    rapidocr: RapidOcrClient, blobs: BlobStore, attachments: TurnAttachmentRepo
) -> dict[str, ToolHandler]:
    """The `ocr` handler, bound to the RapidOCR sidecar client, the blob store, and the
    chat-attachment repo. Wired only when a sidecar is configured; the registry drops the
    tool otherwise (graceful degrade, like the vision/whisper sidecars)."""

    async def ocr_tool(arguments: dict, ctx: ToolContext) -> str:
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        if ctx.agent_session_id is None or not _is_uuid(attachment_id):
            return _NO_IMAGE
        # A chat attachment is domain-scoped, read under the session's attachment context;
        # RLS hides a foreign id as a clean miss.
        att_ctx = await attachments.session_read_context(ctx.session, ctx.agent_session_id)
        if att_ctx is None:
            return _NO_IMAGE
        info = await attachments.get(att_ctx, attachment_id)
        if info is None:
            return _NO_IMAGE
        is_pdf = info.media_type == _PDF_MEDIA_TYPE
        if not (is_pdf or info.media_type.startswith("image/")):
            return _NOT_SUPPORTED
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            return "That file is no longer available."
        try:
            text = (
                await _ocr_pdf(rapidocr, data)
                if is_pdf
                else (await rapidocr.ocr(data, info.media_type)).text
            )
        except OcrServiceError as exc:
            log.warning("ocr_tool_unavailable", error=str(exc))
            return _UNAVAILABLE
        except Exception as exc:  # noqa: BLE001 - an unexpected decode failure → specific, actionable
            # A non-OcrServiceError from the OCR/decode path (e.g. rapidocr raising on malformed
            # bytes). A corrupt PDF that can't even be opened is handled upstream (pdf_page_images
            # returns no pages → _NO_TEXT); this catches the surprises with a clearer message than
            # the loop's generic wrapper.
            log.warning("ocr_tool_decode_failed", media_type=info.media_type, error=repr(exc))
            return (
                "I couldn't read that file — it may be corrupt, encrypted, or password-protected."
                " Try re-exporting or re-attaching it."
            )
        text = text.strip()
        if not text:
            return _NO_TEXT
        return f'Text read from "{info.filename}":\n{text}'

    return {"ocr": ocr_tool}


async def _ocr_pdf(rapidocr: RapidOcrClient, data: bytes) -> str:
    """OCR a PDF page by page, each page anchored, so a multi-page scan reads in order."""
    images = await asyncio.to_thread(pdf_page_images, data)
    parts: list[str] = []
    for number, png in enumerate(images, start=1):
        page = (await rapidocr.ocr(png, "image/png")).text.strip()
        if page:
            parts.append(f"[page {number}]\n{page}")
    return "\n\n".join(parts)
