"""The analyze_video fuse step: grouping transcript words into utterances and
interleaving them with frame captions on one [mm:ss] timeline (pure, no DB/LLM);
plus the chunked long-audio transcription (WAV split + time-shifted merge)."""

import io
import wave

from jbrain.ingest.video import (
    _split_wav,
    build_timeline,
    group_utterances,
    transcribe_audio_chunked,
)
from jbrain.transcribe import Transcript, Word


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


# --- chunked long-audio transcription -----------------------------------------


def _wav(seconds: float, *, rate: int = 16000) -> bytes:
    """A silent 16 kHz mono 16-bit WAV of `seconds` — enough for _split_wav to slice."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_split_wav_slices_by_duration_with_offsets() -> None:
    chunks = _split_wav(_wav(10), chunk_s=4)
    assert [off for off, _ in chunks] == [0, 4000, 8000]  # 4 + 4 + 2 s
    # Each piece is a valid standalone WAV.
    for _, data in chunks:
        with wave.open(io.BytesIO(data), "rb") as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1


def test_split_wav_returns_one_piece_when_short_or_unparseable() -> None:
    assert _split_wav(_wav(2), chunk_s=4) == [(0, _wav(2))]  # shorter than a chunk
    assert _split_wav(b"not a wav", chunk_s=4) == [(0, b"not a wav")]  # degrade, no raise


class _ChunkTranscribe:
    """Returns one word per call, its time relative to the (chunk) audio start, so the
    test can prove the merge shifts each chunk onto the whole-clip timeline."""

    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls += 1
        n = self.calls
        return Transcript(
            text=f"word{n}", words=(Word(f"word{n}", 100, 400, 0.9),), duration_ms=400
        )


async def test_transcribe_audio_chunked_merges_with_shifted_timestamps() -> None:
    fake = _ChunkTranscribe()
    seen: list[tuple[int, int]] = []
    out = await transcribe_audio_chunked(
        fake,
        None,
        "",
        _wav(10),
        filename="a.wav",
        chunk_s=4,  # 3 chunks
        on_progress=lambda step, total, label: seen.append((step, total)),
    )
    assert fake.calls == 3
    assert seen == [(1, 3), (2, 3), (3, 3)]  # per-chunk progress
    assert out is not None
    assert out["text"] == "word1 word2 word3"
    # Chunk k's word (rel start 100ms) lands at k's offset + 100ms on the merged line.
    assert [w["start_ms"] for w in out["words"]] == [100, 4100, 8100]


async def test_transcribe_audio_chunked_survives_a_failing_chunk() -> None:
    class _Flaky(_ChunkTranscribe):
        async def transcribe(self, audio, *, filename, media_type):  # type: ignore[override]
            self.calls += 1
            if self.calls == 2:
                raise OSError("boom")
            return Transcript(text=f"w{self.calls}", words=(), duration_ms=400)

    out = await transcribe_audio_chunked(_Flaky(), None, "", _wav(10), filename="a.wav", chunk_s=4)
    assert out is not None and out["text"] == "w1 w3"  # the failed middle chunk is skipped
