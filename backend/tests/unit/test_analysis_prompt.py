"""Extraction prompt assembly: OCR/caption provenance markers (the Guards
hook — the model can only lower confidence for OCR-derived content if the
prompt says which content that is)."""

from datetime import datetime

from jbrain.analysis.prompt import SYSTEM_PROMPT, build_user_prompt, prompt_block


def test_note_chunks_pass_through_unmarked() -> None:
    assert prompt_block("plain body text", source_kind="note", filename=None) == "plain body text"
    assert prompt_block("page text", source_kind="text-layer", filename="doc.pdf") == "page text"


def test_ocr_and_caption_blocks_announce_their_provenance() -> None:
    assert prompt_block("Total: $41.20", source_kind="ocr", filename="receipt.png") == (
        "[ocr from receipt.png]\nTotal: $41.20"
    )
    assert prompt_block("A receipt.", source_kind="caption", filename="receipt.png") == (
        "[image caption of receipt.png]\nA receipt."
    )
    # A filename should always exist (chunks anchor to it), but the marker
    # degrades honestly rather than KeyError-ing mid-analysis.
    assert prompt_block("x", source_kind="ocr", filename=None).startswith("[ocr from attachment]")


def test_transcript_block_announces_provenance_and_low_confidence() -> None:
    # A clean transcript: provenance only.
    assert (
        prompt_block(
            "Discussed the roadmap.", source_kind="transcript", filename="memo.wav", confidence=0.8
        )
        == "[transcript from memo.wav]\nDiscussed the roadmap."
    )
    # Missing confidence still marks provenance (no qualifier).
    assert prompt_block("hi", source_kind="transcript", filename="memo.wav").startswith(
        "[transcript from memo.wav]"
    )
    # Noisy audio (below the threshold): the model is told to discount it harder.
    assert (
        prompt_block(
            "muffled words", source_kind="transcript", filename="memo.wav", confidence=0.35
        )
        == "[low-confidence transcript from memo.wav]\nmuffled words"
    )


def test_marked_blocks_flow_into_the_user_prompt() -> None:
    prompt = build_user_prompt(
        ["body text", prompt_block("OCR text", source_kind="ocr", filename="scan.png")],
        anchor=datetime.fromisoformat("2026-06-10T17:11:00-06:00"),
        domain="general",
    )
    assert "body text\n\n[ocr from scan.png]\nOCR text" in prompt


def test_system_prompt_keeps_the_ocr_confidence_rule() -> None:
    # prompt_block's marker is only useful while this rule exists; if the
    # wording changes, the marker contract needs rethinking with it.
    assert "OCR-derived" in SYSTEM_PROMPT
