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

from jbrain.ingest.ocr import ocr_pdf_pages
from jbrain.llm import LlmRouter

VISION_OCR_TASK = "vision.ocr"


async def ocr_scanned_pdf(router: LlmRouter, data: bytes, filename: str) -> list[str]:
    """Transcribe each page of a scanned PDF via the shared `vision.ocr` page core
    (`ingest.ocr.ocr_pdf_pages`). The EMR importer's fallback when the ingest OCR job
    has not already cached the transcripts — same route, same result."""
    return await ocr_pdf_pages(router, data, filename)
