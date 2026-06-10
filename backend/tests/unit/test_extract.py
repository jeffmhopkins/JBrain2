"""Extraction dispatcher: text decode, PDF text layer, registry routing."""

import pymupdf

from jbrain.ingest.extract import (
    KIND_TEXT_LAYER,
    PdfTextLayerExtractor,
    Segment,
    TextExtractor,
    default_registry,
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
