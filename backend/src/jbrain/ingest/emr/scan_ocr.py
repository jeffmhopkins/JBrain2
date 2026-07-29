"""Vision-OCR for scanned (zero-text-layer) EMR PDFs (docs/plans/EMR_IMPORT_PLAN.md
§6.2). ARIA exports are page images with no text layer, so the deterministic
`PdfTextLayerExtractor` yields nothing. This renders each page to an image and runs
it through the shipped vision-OCR route (`vision.ocr`) — the ONLY LLM adapter call
on this path — returning one transcript per page. The `emr_parse` handler feeds
that page text to the line-oriented ARIA parser (§6.3) and caches it as `ocr`
attachment extracts so a re-run never re-bills the model.

Reuses the shipped OCR prompt/tier/confidence (`ingest.ocr`) so a scanned EMR page
is transcribed exactly like any other scanned document; the page images ride the
same `downscale_for_vision` cap that keeps a phone-photo-sized scan under the
vision token budget.
"""

from __future__ import annotations

import asyncio
import base64

from jbrain.ingest.imageprep import downscale_for_vision, pdf_page_images
from jbrain.ingest.ocr import OCR_MAX_TOKENS, OCR_STRENGTH, OCR_SYSTEM
from jbrain.llm import LlmRouter
from jbrain.llm.types import LlmImage

VISION_OCR_TASK = "vision.ocr"


async def ocr_scanned_pdf(router: LlmRouter, data: bytes, filename: str) -> list[str]:
    """Transcribe each page of a scanned PDF via the vision-OCR route, in order.
    Returns one (possibly empty) transcript per page; an unreadable PDF yields no
    pages (rasterization degrades to empty rather than raising, §6.2)."""
    images = await asyncio.to_thread(pdf_page_images, data)
    texts: list[str] = []
    for number, png in enumerate(images, start=1):
        prepared, media_type = downscale_for_vision(png, "image/png")
        image = LlmImage(media_type=media_type, data=base64.b64encode(prepared).decode("ascii"))
        result = await router.complete(
            VISION_OCR_TASK,
            system=OCR_SYSTEM,
            user_text=f"Transcribe this scanned page ({filename}, page {number}).",
            images=[image],
            max_tokens=OCR_MAX_TOKENS,
            strength=OCR_STRENGTH,
        )
        texts.append(result.text.strip())
    return texts
