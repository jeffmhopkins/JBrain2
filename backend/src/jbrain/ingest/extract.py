"""Attachment extraction dispatcher (docs/ANALYSIS.md, "the analysis dispatcher").

Every attachment routes by media type to a registered extractor; every
extractor implements the same protocol and returns provenanced segments, so
future backends (OCR, captioning, transcription — Phase 3, they need the LLM
adapter) are registry entries, not new code paths. Media types with no
registered backend extract to nothing rather than failing the pipeline.

Extractors are synchronous CPU work; the pipeline runs them off the event
loop via asyncio.to_thread.
"""

from dataclasses import dataclass
from typing import Protocol, cast

import pymupdf

# Segment kinds mirror app.chunks.source_kind (sans 'note', which is the body).
KIND_TEXT_LAYER = "text-layer"
KIND_OCR = "ocr"
KIND_TRANSCRIPT = "transcript"
KIND_CAPTION = "caption"


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

    Pages without a text layer (scans) produce no segment this phase — the
    vision/OCR backend that would handle them arrives in Phase 3.
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
