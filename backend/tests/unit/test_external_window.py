"""Unit tests for the external-corpus timeline windower (pure, no DB/LLM)."""

from jbrain.external.window import window_timeline


def _word(text: str, start_ms: int) -> dict:
    return {"text": text, "start_ms": start_ms, "end_ms": start_ms + 400, "confidence": 0.9}


def test_windows_carry_first_entry_offset_and_clean_prose() -> None:
    analysis = {
        "duration_ms": 20_000,
        "frames": [{"t_ms": 0, "caption": "A rocket on the pad.", "thumb_id": "a"}],
        "transcript": {"words": [_word("Liftoff", 1_000), _word("confirmed.", 1_400)]},
    }
    windows = window_timeline(analysis)

    assert len(windows) == 1
    w = windows[0]
    assert w.seq == 0
    assert w.t_ms == 0  # the frame (t=0) precedes the speech (t=1000)
    assert w.text == "A rocket on the pad. Liftoff confirmed."
    # No timeline scaffolding leaks into the indexed text.
    for marker in ("[", "(frame)", "(said)", "“", "”"):
        assert marker not in w.text


def test_large_time_gap_starts_a_new_window() -> None:
    analysis = {
        "frames": [],
        "transcript": {
            "words": [_word("Intro", 0), _word("segment.", 400)]
            + [_word("Much", 120_000), _word("later.", 120_400)]
        },
    }
    windows = window_timeline(analysis)

    assert [w.seq for w in windows] == [0, 1]
    assert windows[0].t_ms == 0 and windows[0].text == "Intro segment."
    assert windows[1].t_ms == 120_000 and windows[1].text == "Much later."


def test_char_cap_splits_a_long_run() -> None:
    words = [_word("word", i * 500) for i in range(400)]  # dense, no sentence ends, no gaps
    windows = window_timeline({"frames": [], "transcript": {"words": words}}, target_chars=200)

    assert len(windows) > 1
    assert [w.seq for w in windows] == list(range(len(windows)))
    assert all(len(w.text) <= 220 for w in windows)  # ~cap, allowing the final word to spill
    assert windows[0].t_ms == 0


def test_empty_analysis_yields_no_windows() -> None:
    assert window_timeline({"frames": [], "transcript": None}) == []
    assert window_timeline({}) == []
