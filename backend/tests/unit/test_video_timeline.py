"""The analyze_video fuse step: grouping transcript words into utterances and
interleaving them with frame captions on one [mm:ss] timeline (pure, no DB/LLM)."""

from jbrain.ingest.video import build_timeline, group_utterances


def _words(*items: tuple[str, int]) -> list[dict]:
    """[(text, start_ms), ...] -> the per-word dict shape the fuse step consumes."""
    return [{"text": t, "start_ms": ms, "end_ms": ms + 200, "confidence": 0.9} for t, ms in items]


def test_group_utterances_flushes_on_sentence_end() -> None:
    words = _words(("Hello", 0), ("there.", 300), ("How", 800), ("are", 1000), ("you?", 1200))
    assert group_utterances(words) == [
        {"t_ms": 0, "text": "Hello there."},
        {"t_ms": 800, "text": "How are you?"},
    ]


def test_group_utterances_caps_long_runs_without_punctuation() -> None:
    # 16 unpunctuated words -> two lines (cap is 14), each timestamped at its first word.
    words = _words(*[(f"w{i}", i * 100) for i in range(16)])
    grouped = group_utterances(words)
    assert len(grouped) == 2
    assert grouped[0]["t_ms"] == 0
    assert grouped[1]["t_ms"] == 14 * 100
    assert grouped[0]["text"].split() == [f"w{i}" for i in range(14)]


def test_group_utterances_ignores_blank_tokens() -> None:
    assert group_utterances(_words(("", 0), ("hi.", 100))) == [{"t_ms": 100, "text": "hi."}]


def test_build_timeline_interleaves_frames_and_speech_in_order() -> None:
    frames = [
        {"t_ms": 0, "caption": "A title card.", "thumb_id": "a"},
        {"t_ms": 4000, "caption": "A diagram.", "thumb_id": "b"},
    ]
    words = _words(("First", 1000), ("point.", 1300), ("Second", 5000), ("point.", 5300))
    timeline = build_timeline(frames, words)
    assert timeline.splitlines() == [
        "[00:00] (frame) A title card.",
        "[00:01] (said) “First point.”",
        "[00:04] (frame) A diagram.",
        "[00:05] (said) “Second point.”",
    ]


def test_build_timeline_orders_frame_before_speech_at_same_instant() -> None:
    # What is on screen frames the line said at the same moment: frame sorts first.
    frames = [{"t_ms": 2000, "caption": "A slide.", "thumb_id": "a"}]
    words = _words(("Look", 2000), ("here.", 2200))
    assert build_timeline(frames, words).splitlines() == [
        "[00:02] (frame) A slide.",
        "[00:02] (said) “Look here.”",
    ]


def test_build_timeline_handles_frames_only_and_speech_only() -> None:
    frames = [{"t_ms": 0, "caption": "Only a frame.", "thumb_id": "a"}]
    assert build_timeline(frames, []) == "[00:00] (frame) Only a frame."
    speech = build_timeline([], _words(("Just", 0), ("audio.", 300)))
    assert speech == "[00:00] (said) “Just audio.”"
