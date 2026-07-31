"""Vision-OCR pure parts: prompt doctrine, the confidence caps (Guards),
per-job mode resolution, and the cache rows a pass builds. The handler's
DB/queue round trip — including full-vs-ocr call counts and the on-demand
override — is integration-tested (test_ocr_pg) with the LLM faked."""

import uuid

import pytest

from jbrain.ingest.ocr import (
    DESCRIPTION_CONFIDENCE,
    DESCRIPTION_SYSTEM,
    MAX_OCR_BYTES,
    OCR_CONFIDENCE,
    OCR_SYSTEM,
    RAPIDOCR_TOOL,
    OcrPipeline,
    _agreement,
    build_extract,
    resolve_mode,
)
from jbrain.vision import OcrResult, OcrServiceError


class _FakeRapid:
    """A stand-in RapidOcrClient: returns a canned result, or raises OcrServiceError."""

    def __init__(self, result: OcrResult | None = None, *, error: bool = False) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def ocr(self, data: bytes, media_type: str = "application/octet-stream") -> OcrResult:
        self.calls += 1
        if self._error:
            raise OcrServiceError("sidecar down")
        assert self._result is not None
        return self._result


def _pipeline(rapid: object | None) -> OcrPipeline:
    # Only the RapidOCR helpers are exercised here — the DB/router deps aren't touched, so
    # None is fine (the full round trip is integration-tested in test_ocr_pg).
    return OcrPipeline(None, None, None, None, rapid)  # type: ignore[arg-type]


def test_agreement_is_whitespace_and_case_insensitive() -> None:
    assert _agreement("Total: $41.20", "total:  $41.20\n") == pytest.approx(1.0)
    assert _agreement("", "") == 1.0
    assert _agreement("abc", "") == 0.0
    assert 0.0 < _agreement("hello world", "hello xorld") < 1.0


async def test_rapid_ocr_none_when_sidecar_unconfigured() -> None:
    assert await _pipeline(None)._rapid_ocr(b"img", "image/png") is None


async def test_rapid_ocr_degrades_to_none_on_service_error() -> None:
    fake = _FakeRapid(error=True)
    assert await _pipeline(fake)._rapid_ocr(b"img", "image/png") is None
    assert fake.calls == 1  # it tried, then degraded


async def test_rapid_ocr_returns_the_result() -> None:
    result = OcrResult(text="hello", mean_score=0.9)
    assert await _pipeline(_FakeRapid(result))._rapid_ocr(b"img", "image/png") is result


def test_rapid_row_is_capped_ocr_tagged_rapidocr() -> None:
    row = _pipeline(None)._rapid_row(
        uuid.uuid4(), "personal", "shot.png", "note-1", "exact text", anchor=None
    )
    assert row.kind == "ocr"
    assert row.tool == RAPIDOCR_TOOL
    assert row.confidence == OCR_CONFIDENCE  # health-safety cap preserved regardless of engine
    assert row.text == "exact text"


async def test_rapid_pdf_pages_empty_without_sidecar() -> None:
    assert await _pipeline(None)._rapid_pdf_pages(b"%PDF-1.4 not really") == []


async def test_rapid_pdf_pages_degrades_on_unrasterizable_bytes() -> None:
    # A bad PDF can't rasterize; the cross-check degrades to VLM-only rather than raising.
    assert await _pipeline(_FakeRapid(OcrResult("x", 0.9)))._rapid_pdf_pages(b"not a pdf") == []


def test_ocr_prompt_demands_verbatim_plain_text_and_honesty() -> None:
    """The transcription contract: verbatim, line structure, plain text only,
    honest about illegibility — never a summarizing or guessing prompt."""
    for required in ("verbatim", "line structure", "plain text", "illegib", "never guess"):
        assert required in OCR_SYSTEM


def test_description_prompt_is_salient_and_transcription_independent() -> None:
    """The full-mode description: the salient information itself (the text
    the fact pipeline mines) — never a second transcription, never a
    description of the medium, never speculation beyond the image."""
    assert "do not transcribe" in DESCRIPTION_SYSTEM
    for required in ("objects", "people", "places", "states or conditions", "relationships"):
        assert required in DESCRIPTION_SYSTEM
    # The salient contract: information, not medium; every detail, one
    # sentence each; no UI-chrome narration.
    for required in ("not a description of the medium", "UI chrome", "dates and times"):
        assert required in DESCRIPTION_SYSTEM
    assert "one plain sentence per distinct piece of information" in DESCRIPTION_SYSTEM
    assert "never speculate" in DESCRIPTION_SYSTEM


def test_resolve_mode_prefers_the_payload_override() -> None:
    """The on-demand analyze endpoint sends mode="full" regardless of the
    stored setting; garbage overrides fall back to the configured mode."""
    assert resolve_mode("full", "ocr") == "full"
    assert resolve_mode("ocr", "full") == "ocr"
    assert resolve_mode(None, "ocr") == "ocr"
    assert resolve_mode("everything", "ocr") == "ocr"


def test_build_extract_caps_confidence_per_guards_doctrine() -> None:
    ocr = build_extract(
        attachment_id=uuid.uuid4(),
        domain="health",
        filename="labs.png",
        kind="ocr",
        text="Glucose 92 mg/dL\n",
        tool="xai:grok-4.3",
    )
    description = build_extract(
        attachment_id=uuid.uuid4(),
        domain="health",
        filename="labs.png",
        kind="caption",
        text=" A printed lab report on a counter. ",
        tool="xai:grok-4.3",
    )
    # OCR-derived text never claims more than the Guards cap; a description
    # (kind 'caption') sits lower still.
    assert ocr.confidence == OCR_CONFIDENCE <= 0.7
    assert description.confidence == DESCRIPTION_CONFIDENCE < OCR_CONFIDENCE
    assert ocr.text == "Glucose 92 mg/dL"
    assert description.text == "A printed lab report on a counter."
    # Anchor + tool provenance ride every row (provenanced segments).
    for row in (ocr, description):
        assert row.source_anchor == "labs.png"
        assert row.tool == "xai:grok-4.3"
        assert row.domain_code == "health"


def test_build_extract_keeps_empty_rows_at_zero_confidence() -> None:
    """No legible text still writes the cache row — it is what stops
    re-ingest from re-enqueueing OCR — but claims no confidence."""
    row = build_extract(
        attachment_id=uuid.uuid4(),
        domain="general",
        filename="blur.jpg",
        kind="ocr",
        text="   ",
        tool="xai:grok-4.3",
    )
    assert row.text == ""
    assert row.confidence == 0.0


def test_size_budget_is_a_sane_image_cap() -> None:
    assert MAX_OCR_BYTES == 8 * 1024 * 1024
