"""Extraction dispatcher: text decode, PDF text layer, registry routing, and
the image chain's pure cache read."""

import pymupdf

from jbrain.ingest.extract import (
    KIND_CAPTION,
    KIND_OCR,
    KIND_TEXT_LAYER,
    CachedExtract,
    PdfTextLayerExtractor,
    Segment,
    TextExtractor,
    default_registry,
    image_segments,
    resolve_media_type,
)


def make_pdf(*page_texts: str | None) -> bytes:
    """A real PDF; None makes a page with no text layer (a 'scan')."""
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        if text is not None:
            page.insert_text((72, 72), text)
    return doc.tobytes()


def test_text_extractor_decodes_utf8() -> None:
    segments = TextExtractor().extract("naïve café — привет 😀".encode())
    assert segments == [Segment(kind=KIND_TEXT_LAYER, text="naïve café — привет 😀")]
    assert segments[0].anchor is None


def test_text_extractor_is_best_effort_on_invalid_bytes() -> None:
    segments = TextExtractor().extract(b"ok \xff\xfe bytes")
    assert len(segments) == 1
    assert "ok" in segments[0].text and "bytes" in segments[0].text


def test_text_extractor_skips_empty_content() -> None:
    assert TextExtractor().extract(b"") == []
    assert TextExtractor().extract(b"  \n\t ") == []


def test_pdf_extractor_yields_per_page_segments_with_anchors() -> None:
    data = make_pdf("first page words", "second page words")
    segments = PdfTextLayerExtractor().extract(data)
    assert [s.anchor for s in segments] == ["page 1", "page 2"]
    assert all(s.kind == KIND_TEXT_LAYER for s in segments)
    assert "first page words" in segments[0].text
    assert "second page words" in segments[1].text


def test_pdf_pages_without_text_layer_produce_no_segment() -> None:
    # Page 2 is a scan-like page: nothing recorded, OCR arrives in Phase 3.
    data = make_pdf("has text", None, "also has text")
    segments = PdfTextLayerExtractor().extract(data)
    assert [s.anchor for s in segments] == ["page 1", "page 3"]


def test_registry_routes_by_prefix_and_noops_unknown_media() -> None:
    registry = default_registry()
    assert registry.extract("text/plain", b"hello") == [Segment(kind=KIND_TEXT_LAYER, text="hello")]
    assert registry.extract("text/markdown", b"# hi") == [
        Segment(kind=KIND_TEXT_LAYER, text="# hi")
    ]
    # Unrouted media types are a deliberate no-op, not an error.
    assert registry.extract("image/png", b"\x89PNG") == []
    assert registry.extract("video/mp4", b"...") == []
    assert registry.extract("application/octet-stream", b"???") == []


def test_registry_prefers_longest_matching_prefix() -> None:
    class CsvExtractor:
        def extract(self, data: bytes) -> list[Segment]:
            return [Segment(kind=KIND_TEXT_LAYER, text="csv")]

    registry = default_registry()
    registry.register("text/csv", CsvExtractor())
    assert registry.extract("text/csv", b"a,b")[0].text == "csv"
    assert registry.extract("text/plain", b"plain")[0].text == "plain"


def test_registry_exposes_extractor_lookup() -> None:
    registry = default_registry()
    assert registry.extractor_for("application/pdf") is not None
    assert registry.extractor_for("audio/ogg") is None


def test_resolve_media_type_trusts_a_specific_declared_type() -> None:
    # A real content-type is authoritative — never second-guessed by extension.
    assert resolve_media_type("application/pdf", "report.bin", b"") == "application/pdf"
    assert resolve_media_type("image/png", "x.pdf", b"%PDF-1.7") == "image/png"


def test_resolve_media_type_recovers_a_generic_pdf_by_magic_then_extension() -> None:
    # The field case: a phone uploads a PDF as octet-stream (or nothing). Magic
    # bytes win first; the filename extension is the fallback.
    assert resolve_media_type("application/octet-stream", "labs.pdf", b"%PDF-1.7") == (
        "application/pdf"
    )
    assert resolve_media_type("", "labs.pdf", b"") == "application/pdf"
    assert resolve_media_type(None, "notes.md", b"") == "text/markdown"
    assert resolve_media_type("application/octet-stream", "data.csv", b"a,b\n") == "text/csv"


def test_resolve_media_type_falls_back_to_octet_stream_when_unknown() -> None:
    assert resolve_media_type("", "mystery", b"\x00\x01") == "application/octet-stream"
    assert resolve_media_type("application/octet-stream", "x.xyz", b"") == (
        "application/octet-stream"
    )


def test_resolved_pdf_routes_to_the_pdf_extractor() -> None:
    # The point of resolution: the recovered type reaches an extractor.
    registry = default_registry()
    octet = "application/octet-stream"
    assert registry.extractor_for(octet) is None
    assert registry.extractor_for(resolve_media_type(octet, "labs.pdf", b"%PDF-1.7")) is not None


def test_image_segments_carry_cache_provenance() -> None:
    """The image chain is a pure cache read: kind, anchor, and the capped
    confidence flow straight from attachment_extracts rows into segments."""
    ocr = CachedExtract(kind=KIND_OCR, text=" Total: $41.20 \n", anchor="rcpt.png", confidence=0.7)
    cap = CachedExtract(kind=KIND_CAPTION, text="A receipt.", anchor="rcpt.png", confidence=0.6)
    segments = image_segments([ocr, cap])
    assert segments == [
        Segment(kind=KIND_OCR, text="Total: $41.20", anchor="rcpt.png", confidence=0.7),
        Segment(kind=KIND_CAPTION, text="A receipt.", anchor="rcpt.png", confidence=0.6),
    ]


def test_image_segments_skip_empty_text_rows() -> None:
    # An empty-text row marks "no legible text" in the cache; it must not
    # become a chunk.
    rows = [CachedExtract(kind=KIND_OCR, text="  \n", anchor="blur.jpg", confidence=0.0)]
    assert image_segments(rows) == []
