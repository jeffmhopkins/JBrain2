"""The machine-read provenance markers (the Guards hook — a reader can only hold
OCR-derived content more loosely if the text says which content that is).

The marker's other half is the persona that reads it: `note_ingest.prompt` names these
four headings, and `test_agents.py`'s digest pin is what stops the wording drifting apart
from this contract without a version bump."""

from jbrain.analysis.prompt import prompt_block


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
