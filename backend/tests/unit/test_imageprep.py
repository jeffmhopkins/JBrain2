"""The vision downscaler — keeps oversized images from flooding the model with
tokens, and falls open on anything it can't decode."""

import io

from PIL import Image

from jbrain.ingest.imageprep import MAX_SIDE, downscale_for_vision


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_is_downscaled_to_jpeg_within_cap() -> None:
    data, media_type = downscale_for_vision(_png(4000, 3000), "image/png")
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) == MAX_SIDE
        # Aspect ratio preserved (4000:3000 → 2048:1536).
        assert img.size == (MAX_SIDE, round(MAX_SIDE * 3000 / 4000))


def test_small_image_passes_through_untouched() -> None:
    original = _png(800, 600)
    data, media_type = downscale_for_vision(original, "image/png")
    assert data == original
    assert media_type == "image/png"


def test_undecodable_bytes_fall_open() -> None:
    junk = b"not an image at all"
    data, media_type = downscale_for_vision(junk, "image/jpeg")
    assert data == junk
    assert media_type == "image/jpeg"


# --- the PDF rasterizer's memory ceiling ------------------------------------
#
# The INPUT is capped at 8 MiB (`ocr.MAX_OCR_BYTES`) but the expansion from it was not:
# a scanned PDF that size is routinely 500-2000 CCITT-G4 pages, and every page's PNG was
# retained until the whole document finished. That peak lands in the worker, which has no
# `mem_limit` and shares the unified pool with the resident models — and earlyoom is told to
# `--prefer ^llama-server$`, so the OS backstop would kill a MODEL, not the rasterizer.


def _pdf(pages: int) -> bytes:
    """A minimal multi-page PDF, built with the same library that reads it."""
    import pymupdf

    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=200, height=200)
    return doc.tobytes()


def test_a_long_scan_stops_at_the_page_ceiling() -> None:
    from jbrain.ingest import imageprep

    pages = imageprep.pdf_page_images(_pdf(imageprep.MAX_PDF_PAGES + 25))
    assert len(pages) == imageprep.MAX_PDF_PAGES


def test_a_document_inside_the_ceiling_is_rendered_whole() -> None:
    """The cap must not cost an ordinary document any pages — a lab report is a handful."""
    from jbrain.ingest import imageprep

    assert len(imageprep.pdf_page_images(_pdf(6))) == 6


def test_the_byte_ceiling_stops_a_few_enormous_pages() -> None:
    """A page cap alone still admits pages big enough to matter, so the byte budget is
    checked independently. Exercised by lowering the ceiling rather than by building a
    multi-hundred-megabyte fixture."""
    from jbrain.ingest import imageprep

    original = imageprep.MAX_PDF_RASTER_BYTES
    try:
        imageprep.MAX_PDF_RASTER_BYTES = 1  # any page at all trips it after the first
        pages = imageprep.pdf_page_images(_pdf(10))
    finally:
        imageprep.MAX_PDF_RASTER_BYTES = original
    assert len(pages) == 1  # the first is rendered, then the budget is spent


def test_an_unreadable_pdf_still_degrades_to_no_pages() -> None:
    """The pre-existing contract: a corrupt attachment yields no OCR text rather than
    failing the import batch."""
    from jbrain.ingest import imageprep

    assert imageprep.pdf_page_images(b"not a pdf at all") == []
