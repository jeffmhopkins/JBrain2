"""Vision-OCR pure parts: prompt doctrine, the confidence caps (Guards),
per-job mode resolution, and the cache rows a pass builds. The handler's
DB/queue round trip — including full-vs-ocr call counts and the on-demand
override — is integration-tested (test_ocr_pg) with the LLM faked."""

import uuid

from jbrain.ingest.ocr import (
    DESCRIPTION_CONFIDENCE,
    DESCRIPTION_SYSTEM,
    MAX_OCR_BYTES,
    OCR_CONFIDENCE,
    OCR_SYSTEM,
    build_extract,
    resolve_mode,
)


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
