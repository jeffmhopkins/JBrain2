"""Attachment extraction dispatcher (docs/ANALYSIS.md, "the analysis dispatcher").

Every attachment routes by media type to a registered extractor; every
extractor implements the same protocol and returns provenanced segments, so
future backends (transcription, local Tesseract) are registry entries, not
new code paths. Media types with no registered backend extract to nothing
rather than failing the pipeline.

The image chain is the exception to the bytes-in interface: vision OCR and
captioning run in the async ocr_attachment job (capture-to-searchable never
waits on a cloud LLM), and ingest consumes their cached products via
image_segments — a pure read over app.attachment_extracts, no LLM here.

Extractors are synchronous CPU work; the pipeline runs them off the event
loop via asyncio.to_thread.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, cast

import pymupdf

# Segment kinds mirror app.chunks.source_kind (sans 'note', which is the body).
KIND_TEXT_LAYER = "text-layer"
KIND_OCR = "ocr"
KIND_TRANSCRIPT = "transcript"
KIND_CAPTION = "caption"

# A declared content-type we cannot trust to route: mobile file pickers commonly
# upload a real PDF as one of these (or as nothing), which would otherwise reach
# no extractor and never index. Magic bytes and the filename extension recover it.
_GENERIC_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_MAGIC_TYPES: tuple[tuple[bytes, str], ...] = ((b"%PDF-", "application/pdf"),)
_EXT_MEDIA: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
}


def resolve_media_type(declared: str | None, filename: str | None, header: bytes = b"") -> str:
    """The best routable media type from the client's declared content-type, the
    file's magic bytes, then its extension. A specific declared type is trusted
    as-is; only a generic/absent one (`application/octet-stream`, "") falls back —
    so a PDF a phone uploads without a proper content-type still reaches the PDF
    extractor instead of being silently dropped from indexing."""
    declared = (declared or "").strip()
    if declared and declared.lower() not in _GENERIC_TYPES:
        return declared
    for magic, media in _MAGIC_TYPES:
        if header.startswith(magic):
            return media
    ext = PurePosixPath(filename or "").suffix.lower()
    return _EXT_MEDIA.get(ext) or declared or "application/octet-stream"


@dataclass(frozen=True)
class Segment:
    """Extracted text with provenance: where in the media it came from."""

    kind: str
    text: str
    anchor: str | None = None  # e.g. "page 3", "02:13" — None for whole-file
    confidence: float = 1.0


class ExtractorProtocol(Protocol):
    def extract(self, data: bytes) -> list[Segment]: ...


class TextExtractor:
    """text/*: best-effort UTF-8 decode; lossy bytes are replaced, not fatal."""

    def extract(self, data: bytes) -> list[Segment]:
        body = data.decode("utf-8", errors="replace").strip()
        if not body:
            return []
        return [Segment(kind=KIND_TEXT_LAYER, text=body)]


class PdfTextLayerExtractor:
    """application/pdf: per-page text layer via PyMuPDF.

    TODO(vision): pages without a text layer (scans) still produce no
    segment. The spec routes them through the image chain (docs/ANALYSIS.md
    "Attachments": pages without one render to images -> image chain); doing
    that here means per-page rows in the attachment_extracts cache and a
    page-aware ocr_attachment job — a follow-up, not a registry tweak.
    """

    def extract(self, data: bytes) -> list[Segment]:
        segments: list[Segment] = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for number in range(1, doc.page_count + 1):
                # get_text's return type varies by option; "text" is always str.
                page_text = cast(str, doc.load_page(number - 1).get_text("text")).strip()
                if page_text:
                    segments.append(
                        Segment(kind=KIND_TEXT_LAYER, text=page_text, anchor=f"page {number}")
                    )
        return segments


@dataclass(frozen=True)
class CachedExtract:
    """One app.attachment_extracts row, as the image chain consumes it."""

    kind: str
    text: str
    anchor: str | None
    confidence: float


def image_segments(extracts: Iterable[CachedExtract]) -> list[Segment]:
    """The image chain: provenanced segments from the vision-extract cache.

    Pure cache read — the ocr_attachment job is the only thing that ever
    calls a vision model. An empty-text row (an image with no legible text)
    yields no segment, but its presence in the cache is still what keeps
    re-ingest from re-enqueueing OCR.
    """
    return [
        Segment(kind=e.kind, text=e.text.strip(), anchor=e.anchor, confidence=e.confidence)
        for e in extracts
        if e.text.strip()
    ]


class ExtractorRegistry:
    """Routes media types to extractors by longest matching prefix.

    Prefix keys ("text/") catch families; exact keys ("application/pdf")
    catch single types. Unrouted media extracts to [] by design.
    """

    def __init__(self) -> None:
        self._extractors: dict[str, ExtractorProtocol] = {}

    def register(self, media_type_prefix: str, extractor: ExtractorProtocol) -> None:
        self._extractors[media_type_prefix] = extractor

    def extractor_for(self, media_type: str) -> ExtractorProtocol | None:
        candidates = [p for p in self._extractors if media_type.startswith(p)]
        if not candidates:
            return None
        return self._extractors[max(candidates, key=len)]

    def extract(self, media_type: str, data: bytes) -> list[Segment]:
        extractor = self.extractor_for(media_type)
        return [] if extractor is None else extractor.extract(data)


def default_registry() -> ExtractorRegistry:
    registry = ExtractorRegistry()
    registry.register("text/", TextExtractor())
    registry.register("application/pdf", PdfTextLayerExtractor())
    return registry
