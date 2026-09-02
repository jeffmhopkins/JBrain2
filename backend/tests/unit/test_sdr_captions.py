"""The live-caption stream: framing, squelch handling, and the residency rule.

Whisper is not a streaming model, so captions are segments of live audio transcribed
one after another. What matters here is that the frames the sidecar sends are parsed
back into whole clips, that one bad segment does not end the stream, and — the point
of the whole route — that the model is NOT freed between segments.
"""

from __future__ import annotations

import io
import json
import wave
from typing import Any

import pytest

from jbrain.api.sdr import _Backlog, _clip_seconds, _event, _segments


class _Upstream:
    """An httpx response that yields the given blocks, however they are chopped up."""

    def __init__(self, blocks: list[bytes]) -> None:
        self._blocks = blocks

    async def aiter_bytes(self):
        for block in self._blocks:
            yield block


def _frame(started: float, wav: bytes) -> bytes:
    head = json.dumps({"started_at": started, "bytes": len(wav)}).encode()
    return head + b"\n" + wav


async def _collect(blocks: list[bytes]) -> list[tuple[float, Any]]:
    return [pair async for pair in _segments(_Upstream(blocks))]  # type: ignore[arg-type]


async def test_a_segment_is_reassembled_from_its_frame() -> None:
    got = await _collect([_frame(12.5, b"RIFFfake")])

    assert got == [(12.5, b"RIFFfake")]


async def test_a_segment_split_across_reads_is_still_whole() -> None:
    frame = _frame(3.0, b"RIFF" + b"\x00" * 40)

    # The WAV is binary and arrives in whatever blocks the socket hands over; a parser
    # that assumed one read per frame would hand whisper truncated audio and get
    # confident nonsense back.
    got = await _collect([frame[:9], frame[9:20], frame[20:]])

    assert got == [(3.0, b"RIFF" + b"\x00" * 40)]


async def test_two_segments_in_one_read_are_both_delivered() -> None:
    got = await _collect([_frame(1.0, b"one") + _frame(2.0, b"two")])

    assert got == [(1.0, b"one"), (2.0, b"two")]


async def test_a_keepalive_is_not_mistaken_for_audio() -> None:
    # A quiet channel sends nothing for a long time, so the sidecar holds the socket
    # open with a keepalive. Transcribing that would be transcribing nothing.
    got = await _collect([b'{"keepalive":true}\n' + _frame(5.0, b"RIFF")])

    assert got == [(0.0, None), (5.0, b"RIFF")]


async def test_a_partial_frame_waits_rather_than_transcribing_half_a_clip() -> None:
    frame = _frame(1.0, b"RIFF12345678")

    got = await _collect([frame[: len(frame) - 4]])

    assert got == []


def test_events_are_server_sent_frames() -> None:
    assert _event({"text": "seas two to three feet"}).endswith("\n\n")
    assert json.loads(_event({"text": "hi"}).removeprefix("data: ").strip()) == {"text": "hi"}


@pytest.mark.parametrize("bad", [b"not json\n", b"\n"])
async def test_a_garbled_header_is_skipped_not_fatal(bad: bytes) -> None:
    # One malformed frame must not end the caption stream: the next transmission is a
    # fresh chance, and the owner is still listening to the radio either way.
    got = await _collect([bad + _frame(9.0, b"RIFF")])

    assert got == [(9.0, b"RIFF")]


def test_the_route_never_frees_the_model_between_segments() -> None:
    """The residency rule, pinned as source because it is invisible at runtime.

    Measured on the box across 26 consecutive calls with the model resident: ~9.8 s
    each, whatever the clip holds. That is inference — whisper.cpp pads every clip to a
    30 s window — and NOT the model loading, as an earlier reading of the same flat
    number concluded. Unloading would therefore add the load on top of the 9.8 s rather
    than being most of it. The capture route does unload, which is right for a one-shot;
    a captioner doing the same falls further behind every segment. If an `unload` ever
    appears in this route, captions are broken in a way no test of the output would
    show.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src/jbrain/api/sdr.py"
    captions = source.read_text().split('@router.get("/captions")')[1]
    body = "\n".join(line for line in captions.splitlines() if not line.strip().startswith("#"))
    # A CALL, not the word — the docstring above explains the rule and would otherwise
    # trip its own guard.
    assert ".unload(" not in body
    assert "free_model" not in body


# --- the backlog -------------------------------------------------------------------
# Reading the sidecar in step with transcribing is what put captions permanently behind
# the audio: every whisper call stalled the reader, the queue filled behind it, and the
# captioner never caught up. These pin the shape that fixed it — read freely, and
# transcribe whatever has piled up as one clip.


def _wav(seconds: float, rate: int = 16_000, fill: bytes = b"\x01\x00") -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(fill * int(seconds * rate))
    return buf.getvalue()


def test_an_empty_backlog_has_nothing_to_transcribe() -> None:
    assert _Backlog().take() is None


def test_one_waiting_segment_is_handed_over_untouched() -> None:
    backlog = _Backlog()
    clip = _wav(4)
    backlog.add(11.0, clip)

    # Byte-identical, not merely equivalent: the common case must not pay a re-encode.
    assert backlog.take() == (11.0, clip)


def test_segments_that_piled_up_are_transcribed_as_one_clip() -> None:
    backlog = _Backlog()
    backlog.add(100.0, _wav(4))
    backlog.add(104.0, _wav(3))
    backlog.add(107.0, _wav(5))

    taken = backlog.take()
    assert taken is not None
    started, wav = taken

    # Whisper costs the same for 4 s as for 12 s on this box, so merging is free — and
    # unlike taking only the newest it drops no words. The stamp is the FIRST segment's
    # start, because that is when the audio the caption describes begins.
    assert started == 100.0
    assert _clip_seconds(wav) == pytest.approx(12.0, abs=0.01)


def test_taking_clears_what_was_taken() -> None:
    backlog = _Backlog()
    backlog.add(1.0, _wav(2))
    backlog.take()

    assert backlog.take() is None


def test_the_backlog_gives_up_its_oldest_rather_than_growing() -> None:
    backlog = _Backlog(max_seconds=10.0)
    for i in range(6):
        backlog.add(float(i * 4), _wav(4))

    taken = backlog.take()
    assert taken is not None
    started, wav = taken

    # A caption for something said half a minute ago is not a live caption. What gives
    # way is the OLDEST audio — dropping the newest instead would leave the captioner
    # reading history while the live edge went past unseen.
    assert _clip_seconds(wav) <= 12.0
    assert started >= 12.0


def test_a_truncated_clip_does_not_poison_the_batch() -> None:
    backlog = _Backlog()
    backlog.add(1.0, _wav(2))
    backlog.add(3.0, b"RIFF truncated")
    backlog.add(4.0, _wav(2))

    taken = backlog.take()
    assert taken is not None

    # One unreadable clip must not cost the transmission around it: the caption stream
    # is live, and there is no second chance at the audio.
    assert _clip_seconds(taken[1]) == pytest.approx(4.0, abs=0.01)

