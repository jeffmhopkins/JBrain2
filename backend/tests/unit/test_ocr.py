"""Vision-OCR pure parts: prompt doctrine, the confidence cap (Guards), and
the cache rows one pass builds. The handler's DB/queue round trip is
integration-tested (test_ocr_pg) with the LLM faked."""

import uuid

from jbrain.ingest.ocr import (
    CAPTION_CONFIDENCE,
    CAPTION_SYSTEM,
    MAX_OCR_BYTES,
    OCR_CONFIDENCE,
    OCR_SYSTEM,
    build_extracts,
)


def test_ocr_prompt_demands_verbatim_plain_text_and_honesty() -> None:
    """The transcription contract: verbatim, line structure, plain text only,
    honest about illegibility — never a summarizing or guessing prompt."""
    for required in ("verbatim", "line structure", "plain text", "illegib", "never guess"):
        assert required in OCR_SYSTEM
    assert "one plain-text sentence" in CAPTION_SYSTEM


def test_build_extracts_caps_confidence_per_guards_doctrine() -> None:
    rows = build_extracts(
        attachment_id=uuid.uuid4(),
        domain="health",
        filename="labs.png",
        ocr_text="Glucose 92 mg/dL\n",
        caption_text=" A printed lab report. ",
        ocr_tool="xai:grok-4.3",
        caption_tool="xai:grok-4.3",
    )
    by_kind = {r.kind: r for r in rows}
    assert set(by_kind) == {"ocr", "caption"}
    # OCR-derived text never claims more than the Guards cap.
    assert by_kind["ocr"].confidence == OCR_CONFIDENCE <= 0.7
    assert by_kind["caption"].confidence == CAPTION_CONFIDENCE < OCR_CONFIDENCE
    assert by_kind["ocr"].text == "Glucose 92 mg/dL"
    assert by_kind["caption"].text == "A printed lab report."
    # Anchor + tool provenance ride every row (provenanced segments).
    assert all(r.source_anchor == "labs.png" for r in rows)
    assert all(r.tool == "xai:grok-4.3" for r in rows)
    assert all(r.domain_code == "health" for r in rows)


def test_build_extracts_keeps_empty_rows_at_zero_confidence() -> None:
    """No legible text still writes the cache row — it is what stops
    re-ingest from re-enqueueing OCR — but claims no confidence."""
    rows = build_extracts(
        attachment_id=uuid.uuid4(),
        domain="general",
        filename="blur.jpg",
        ocr_text="   ",
        caption_text="",
        ocr_tool="xai:grok-4.3",
        caption_tool="xai:grok-4.3",
    )
    assert [r.text for r in rows] == ["", ""]
    assert [r.confidence for r in rows] == [0.0, 0.0]


def test_size_budget_is_a_sane_image_cap() -> None:
    assert MAX_OCR_BYTES == 8 * 1024 * 1024
