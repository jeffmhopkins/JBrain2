"""The transcribe job's pure confidence aggregation: the extract row's confidence
is the words' mean, capped at the Guards ceiling (noisy audio reads low)."""

import pytest

from jbrain.ingest.transcribe_job import TRANSCRIPT_CONFIDENCE, transcript_confidence


def _words(*confidences: float) -> list[dict[str, object]]:
    return [{"text": "w", "start_ms": 0, "end_ms": 1, "confidence": c} for c in confidences]


def test_clean_audio_is_capped_at_the_ceiling() -> None:
    # Mean of high confidences exceeds the ceiling → capped.
    assert transcript_confidence(_words(0.95, 0.97, 0.9)) == TRANSCRIPT_CONFIDENCE


def test_noisy_audio_reads_below_the_ceiling() -> None:
    assert transcript_confidence(_words(0.3, 0.5, 0.4)) == pytest.approx(0.4)


def test_no_words_falls_back_to_the_flat_ceiling() -> None:
    assert transcript_confidence([]) == TRANSCRIPT_CONFIDENCE
