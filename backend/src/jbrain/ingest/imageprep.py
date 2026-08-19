"""Downscale oversized images before they reach a vision model.

A phone photo (several thousand px per side) becomes thousands of vision tokens:
slow to encode and enough to overflow a modest context window, which is what
wedged local OCR into a timeout/retry loop. Capping the long side keeps text
legible for OCR while cutting the token count sharply. Decode failures fall open
— the original bytes pass through untouched so a format Pillow can't read still
reaches the model.
"""

from __future__ import annotations

import io

import pymupdf
import structlog
from PIL import Image, ImageOps, UnidentifiedImageError

log = structlog.get_logger()

# Long-side cap: large enough that document text stays readable, small enough
# that a 4000px photo stops producing thousands of image tokens.
MAX_SIDE = 2048
# Re-encode quality — high enough to preserve OCR legibility.
JPEG_QUALITY = 90
# Rasterization DPI for a scanned PDF page: high enough that small lab-table type
# survives OCR, low enough to stay under the vision size cap after downscaling.
PDF_RENDER_DPI = 200

# Ceilings on what one document may rasterize into memory at once. The INPUT is already
# capped (`ocr.MAX_OCR_BYTES`, 8 MiB) but the expansion from it is not bounded by anything:
# a scanned PDF is CCITT-G4/JBIG2, so 8 MiB is routinely 500-2000 pages, and at 200 DPI a
# letter page is 1700x2200 — an ~11 MiB pixmap that retains as a ~0.5-3 MiB PNG. Every page
# was held until the whole document finished, so the peak was pages x that, in the worker,
# which has no `mem_limit` and shares the unified pool with up to ~91 GB of resident models.
# earlyoom would not even have helped: it is told to `--prefer ^llama-server$`, so the OS
# backstop kills a MODEL rather than the runaway rasterizer.
#
# Both bounds, not one: a page cap alone still admits a few enormous pages, and a byte cap
# alone still walks a thousand of them. Truncating beats refusing — OCR of the first pages of
# a huge scan is worth more than nothing — but it is LOGGED, because silently reading half a
# document is the kind of thing that should never be discovered from a wrong answer.
MAX_PDF_PAGES = 200
MAX_PDF_RASTER_BYTES = 512 * 1024 * 1024


def pdf_page_images(data: bytes) -> list[bytes]:
    """Render each page of a (scanned, text-less) PDF to PNG bytes for vision OCR.

    The one PDF-page rasterizer in the codebase (the text path uses `get_text`).
    Synchronous CPU work — the caller runs it off the event loop. An unreadable
    PDF yields no pages rather than raising, so a corrupt attachment degrades to
    'no OCR text' instead of failing the import batch.

    Bounded by `MAX_PDF_PAGES` and `MAX_PDF_RASTER_BYTES`: a document past either stops
    there and reports how far it got."""
    pages: list[bytes] = []
    total = 0
    stopped_at: int | None = None
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            page_count = doc.page_count
            # Indexed rather than `enumerate(doc)`: pymupdf's Document is subscriptable but
            # not typed as Iterable, so iterating it directly fails the typecheck.
            for index in range(page_count):
                if index >= MAX_PDF_PAGES or total >= MAX_PDF_RASTER_BYTES:
                    stopped_at = index
                    break
                png = doc[index].get_pixmap(dpi=PDF_RENDER_DPI).tobytes("png")
                total += len(png)
                pages.append(png)
    except Exception as exc:  # noqa: BLE001 — a bad scan must not fail the batch
        log.info("vision.pdf_rasterize_skipped", error=str(exc))
        return pages
    if stopped_at is not None:
        log.warning(
            "vision.pdf_rasterize_truncated",
            rendered=stopped_at,
            page_count=page_count,
            bytes_rendered=total,
        )
    return pages


def downscale_for_vision(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Return (possibly smaller) image bytes + media type for a vision call.

    No-op (returns the input unchanged) when the image is already within
    `MAX_SIDE` or can't be decoded."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)  # honor camera rotation before measuring
            width, height = img.size
            if max(width, height) <= MAX_SIDE:
                return data, media_type
            scale = MAX_SIDE / max(width, height)
            resized = img.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.Resampling.LANCZOS,
            )
            if resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=JPEG_QUALITY)
    except (UnidentifiedImageError, OSError) as exc:
        log.info("vision.downscale_skipped", media_type=media_type, error=str(exc))
        return data, media_type
    return buf.getvalue(), "image/jpeg"
