"""Scanned-PDF vision-OCR for the EMR importer (docs/plans/EMR_IMPORT_PLAN.md §6.2):
the page rasterizer and the per-page `vision.ocr` pass (LLM faked, non-neg #1/#5).
"""

from __future__ import annotations

import pymupdf

from jbrain.ingest.emr.scan_ocr import VISION_OCR_TASK, ocr_scanned_pdf
from jbrain.ingest.imageprep import pdf_page_images
from jbrain.llm import FakeLlmClient, LlmRouter


def _pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"scan page {n + 1}")
    out = doc.tobytes()
    doc.close()
    return out


def test_rasterizer_renders_one_png_per_page() -> None:
    imgs = pdf_page_images(_pdf(3))
    assert len(imgs) == 3
    assert all(png.startswith(b"\x89PNG\r\n") for png in imgs)  # real PNG bytes


def test_rasterizer_tolerates_a_non_pdf() -> None:
    assert pdf_page_images(b"not a pdf at all") == []  # degrades to no pages, never raises


def _router(*responses: str) -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(responses=responses or ("ocr",))
    router = LlmRouter(
        {"xai": fake}, {VISION_OCR_TASK: ("xai", "grok")}, tiers={"vision": ("xai", "grok")}
    )
    return router, fake


async def test_ocr_transcribes_each_page_in_order() -> None:
    router, fake = _router("PAGE ONE TEXT", "PAGE TWO TEXT")
    pages = await ocr_scanned_pdf(router, _pdf(2), "records.pdf")
    assert pages == ["PAGE ONE TEXT", "PAGE TWO TEXT"]
    # One vision.ocr call per page, each carrying exactly one image.
    assert len(fake.calls) == 2
    assert all(len(c["images"]) == 1 for c in fake.calls)


async def test_ocr_of_an_unreadable_pdf_calls_nothing() -> None:
    router, fake = _router("unused")
    assert await ocr_scanned_pdf(router, b"garbage", "x.pdf") == []
    assert fake.calls == []  # no pages -> no model calls
