"""The live-caption stream: framing, squelch handling, and the residency rule.

Whisper is not a streaming model, so captions are segments of live audio transcribed
one after another. What matters here is that the frames the sidecar sends are parsed
back into whole clips, that one bad segment does not end the stream, and — the point
of the whole route — that the model is NOT freed between segments.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jbrain.api.sdr import _event, _segments


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

    Measured on the box: a transcription costs ~10.7 s whether the clip is 4 s or 11 s
    — almost all of it loading and unloading the model, with 7 extra seconds of audio
    adding 0.04 s. The capture route unloads when it finishes, which is right for a
    one-shot; a captioner doing the same would pay that per segment and fall
    permanently behind. If an `unload` ever appears in this route, captions are broken
    in a way no test of the output would show.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src/jbrain/api/sdr.py"
    captions = source.read_text().split('@router.get("/captions")')[1]
    body = "\n".join(line for line in captions.splitlines() if not line.strip().startswith("#"))
    # A CALL, not the word — the docstring above explains the rule and would otherwise
    # trip its own guard.
    assert ".unload(" not in body
    assert "free_model" not in body
