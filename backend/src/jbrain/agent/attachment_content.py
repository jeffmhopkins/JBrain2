"""Turning a chat turn's pre-uploaded attachments into adapter-agnostic LLM content.

Given the session's narrowed read context and the ordered attachment ids the turn
referenced, this returns `(images, extra_text)`: vision images for the model plus a
labeled text block to append to the user message. The caller rides them ONLY on the
new turn (history stays text — past images are never re-sent).

Binding decisions (owner): images go straight to vision; a PDF is BOTH rasterized
per page (each page → a PNG LlmImage) AND has its text layer extracted; known-text
files are decoded inline. Everything is fetched under the session's firewall via
TurnAttachmentRepo.get, so an out-of-scope or unknown id is invisible (it reads as
missing) and is skipped rather than crashing the turn (CLAUDE.md rules 2 & 3).

The rasterize/extract work is synchronous CPU work (PyMuPDF), so it runs off the
event loop via asyncio.to_thread.
"""

import asyncio
import base64
import logging
import math
from dataclasses import dataclass

import pymupdf

from jbrain.agent.attachments import AttachmentInfo, TurnAttachmentRepo
from jbrain.db.session import SessionContext
from jbrain.llm import LlmImage
from jbrain.storage import BlobStore

_log = logging.getLogger(__name__)

# How many attachment ids one turn may reference — a graceful cap mirrored by the
# request validator. Keeps a turn from ballooning the context with dozens of files.
MAX_ATTACHMENTS_PER_TURN = 10
# Per PDF, how many pages we rasterize + extract. A long PDF is truncated (with a
# note appended) rather than flooding the vision context.
MAX_PDF_PAGES = 10
# The overall image cap for one turn (images from images + PDF pages combined), so a
# handful of multi-page PDFs can't exceed what the vision model should receive.
MAX_IMAGES_PER_TURN = 20
# Pages render at this zoom (1.0 = 72 dpi); ~1.5x keeps text legible without bloating
# the base64 payload.
_PDF_RENDER_ZOOM = 1.5
# The ceiling on a rendered page's pixel area (~4 MP). A malicious PDF can declare a
# huge MediaBox (a 522-byte file → a 21600x21600 ≈ 2.7 GB pixmap) that the page-count
# and byte caps don't bound; the per-page zoom is floored so the pixmap never exceeds
# this budget (SECURITY: rasterization DoS).
MAX_PDF_PAGE_PIXELS = 4_000_000


@dataclass(frozen=True)
class _Converted:
    images: list[LlmImage]
    text_blocks: list[str]


def _image_block(info: AttachmentInfo, data: bytes) -> _Converted:
    image = LlmImage(media_type=info.media_type, data=_b64(data))
    # Name the attachment's id alongside the vision content so the model can act on it
    # by reference even when it can't see the bytes (a text-only agent model): pass the
    # id as source_attachment_id to analyze_image to look at it or edit_image to change it.
    note = (
        f'[attached image "{info.filename}" — its id is {info.id}: pass it as '
        "source_attachment_id to analyze_image to look at it or edit_image to change it]"
    )
    return _Converted(images=[image], text_blocks=[note])


def _text_block(info: AttachmentInfo, data: bytes) -> _Converted:
    body = data.decode("utf-8", errors="replace").strip()
    if not body:
        return _Converted(images=[], text_blocks=[])
    return _Converted(images=[], text_blocks=[f"[{info.filename}]:\n{body}"])


def _pdf_block(info: AttachmentInfo, data: bytes, image_budget: int) -> _Converted:
    """Each page (up to MAX_PDF_PAGES and the remaining image budget) → a PNG image
    for vision AND its extracted text layer. Synchronous PyMuPDF work — the caller
    runs it via asyncio.to_thread."""
    images: list[LlmImage] = []
    text_blocks: list[str] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        page_cap = min(doc.page_count, MAX_PDF_PAGES)
        for number in range(1, page_cap + 1):
            page = doc.load_page(number - 1)
            if len(images) < image_budget:
                png = page.get_pixmap(matrix=_page_matrix(page)).tobytes("png")
                images.append(LlmImage(media_type="image/png", data=_b64(png)))
            page_text = page.get_text("text").strip()  # type: ignore[no-untyped-call]
            if page_text:
                text_blocks.append(f"[{info.filename}, page {number}]:\n{page_text}")
        if doc.page_count > page_cap:
            text_blocks.append(
                f"[{info.filename}]: showing the first {page_cap} of {doc.page_count} pages."
            )
    return _Converted(images=images, text_blocks=text_blocks)


def _page_matrix(page: pymupdf.Page) -> pymupdf.Matrix:
    """The render matrix for one page: the base zoom, floored so the rasterized pixel
    area stays within MAX_PDF_PAGE_PIXELS. The zoom is only ever REDUCED — a normal
    page renders at the base zoom; an oversized MediaBox is scaled down so it can't
    blow up the pixmap (SECURITY: rasterization DoS). Pixels scale with zoom², so the
    cap is sqrt(budget / area_in_points)."""
    area_pt = max(page.rect.width * page.rect.height, 1.0)
    zoom = min(_PDF_RENDER_ZOOM, math.sqrt(MAX_PDF_PAGE_PIXELS / area_pt))
    zoom = max(zoom, 1e-3)  # keep it positive even for an absurdly large page
    return pymupdf.Matrix(zoom, zoom)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _convert_one(info: AttachmentInfo, data: bytes, image_budget: int) -> _Converted:
    """Route one attachment to its conversion by media type. CPU-bound for PDFs, so
    the caller invokes this off the event loop (asyncio.to_thread)."""
    if info.media_type.startswith("image/"):
        return _image_block(info, data) if image_budget > 0 else _Converted([], [])
    if info.media_type == "application/pdf":
        return _pdf_block(info, data, image_budget)
    # Everything else reaching here is a known-text type (the upload allowlist gates
    # the set); decode it inline.
    return _text_block(info, data)


async def build_attachment_content(
    repo: TurnAttachmentRepo,
    blobs: BlobStore,
    ctx: SessionContext,
    attachment_ids: list[str],
) -> tuple[list[LlmImage], str]:
    """`(images, extra_text)` for the turn's attachments, in request order.

    Each id is fetched under the session's narrowed firewall (`repo.get(ctx, id)`):
    an out-of-scope or unknown id reads as missing and is SKIPPED (a stray id must
    never crash the turn — the model just doesn't see that file). Bytes come from the
    blob store (CLAUDE.md rule 2). Images and PDF pages share one image budget
    (MAX_IMAGES_PER_TURN); text blocks are joined into one appended section.
    """
    images: list[LlmImage] = []
    text_blocks: list[str] = []
    for attachment_id in attachment_ids[:MAX_ATTACHMENTS_PER_TURN]:
        info = await repo.get(ctx, attachment_id)
        if info is None:
            continue  # out-of-scope or unknown — invisible to the turn, not an error
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            continue  # the row outlived its blob (rare) — skip rather than break the turn
        budget = MAX_IMAGES_PER_TURN - len(images)
        try:
            converted = await asyncio.to_thread(_convert_one, info, data, budget)
        except Exception:
            # A corrupt/encrypted/otherwise-unreadable file must not abort the turn or
            # the other attachments (the docstring's "skipped rather than crashing").
            # Same graceful path as a missing id, plus a minimal note so the model knows
            # something was dropped. SECURITY/robustness: build runs BEFORE the stream's
            # try/finally, so an unhandled raise here 500s the turn and dangles a run-log.
            _log.warning("attachment %s could not be read; skipping", info.id, exc_info=True)
            text_blocks.append(f"[{info.filename}]: could not be read.")
            continue
        images.extend(converted.images[: MAX_IMAGES_PER_TURN - len(images)])
        text_blocks.extend(converted.text_blocks)
    extra_text = ("\n\n".join(text_blocks)).strip()
    return images, extra_text
