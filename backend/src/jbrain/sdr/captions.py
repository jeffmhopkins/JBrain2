"""Whisper's half of the live radio: WAV segments off the sidecar, and the backlog.

Whisper is not a streaming model — it transcribes a finished clip — so the sidecar cuts
the live audio into segments on the quiet gaps between transmissions
(`deploy/sdr/listen.py`) and serves them newline-framed at `GET /listen/segments`. This
module is the reading of that frame and the queue in front of the model, and nothing
else: no HTTP client, no settings, no route.

**It lives here rather than in `api/sdr.py` because there are now two captioners.** The
SSE route pushes each caption at the owner as it lands; the RECORDER keeps them and
writes a transcript row (`recorder.py`, `kind='captions'`). One framing parser for both
is the point — a second copy is a second thing to get wrong about a stream whose frames
carry a length prefix, and the failure mode of getting it wrong is a WAV read at the
wrong offset and handed to a model that answers anything at all.

Each captioner subscribes SEPARATELY on the sidecar (`Session.subscribe_segments` fans
out to a queue per subscriber), so recording captions neither steals the live stream's
segments nor depends on a browser tab staying open. The cost is that whisper transcribes
the same audio twice while both run; `Backlog` is what makes that survivable, because a
captioner that falls behind merges what is waiting into ONE clip rather than working
through them in order for ever.
"""

from __future__ import annotations

import io
import json
import wave
from collections.abc import AsyncIterator

import httpx

# How far back a single caption may reach. Segments that pile up behind a busy whisper
# are transcribed TOGETHER rather than one at a time (see Backlog), and this bounds how
# much audio one merged clip may carry: past it the oldest is given up, because a caption
# for something said half a minute ago is not a live caption any more.
CAPTION_BACKLOG_S = 24.0


async def segments(upstream: httpx.Response) -> AsyncIterator[tuple[float, bytes | None]]:
    """Split the sidecar's newline-framed stream into (started_at, wav) pairs.

    The frame is a JSON header line then exactly `bytes` of WAV. Framing rather than a
    request per segment because the gap between requests always lands mid-sentence."""
    buffer = b""
    async for block in upstream.aiter_bytes():
        buffer += block
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            try:
                head = json.loads(buffer[:newline] or b"{}")
            except json.JSONDecodeError:
                buffer = buffer[newline + 1 :]
                continue
            if head.get("keepalive"):
                buffer = buffer[newline + 1 :]
                yield 0.0, None
                continue
            size = int(head.get("bytes", 0))
            if size <= 0:
                # Not a segment frame — a blank line, or a header that lost its size.
                # Yielding it would hand whisper zero bytes of audio to describe.
                buffer = buffer[newline + 1 :]
                continue
            if len(buffer) < newline + 1 + size:
                break  # the WAV has not all arrived yet
            wav = buffer[newline + 1 : newline + 1 + size]
            buffer = buffer[newline + 1 + size :]
            yield float(head.get("started_at", 0.0)), wav


def clip_seconds(wav: bytes) -> float:
    """How much audio a WAV clip holds, without reading its samples."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as src:
            return src.getnframes() / (src.getframerate() or 1)
    except (wave.Error, EOFError, OSError):
        return 0.0


def merge(clips: list[bytes]) -> bytes:
    """Join consecutive WAV clips into one.

    The clips are contiguous slices of the same live capture, so concatenating their
    frames reproduces the audio exactly as it was on the air — there is no crossfade or
    resample to get wrong."""
    frames: list[bytes] = []
    rate = 0
    for clip in clips:
        try:
            with wave.open(io.BytesIO(clip), "rb") as src:
                frames.append(src.readframes(src.getnframes()))
                rate = src.getframerate() or rate
        except (wave.Error, EOFError, OSError):
            continue  # a truncated clip is dropped, not allowed to poison the batch
    if not frames or not rate:
        return clips[0] if clips else b""
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(b"".join(frames))
    return out.getvalue()


class Backlog:
    """Segments waiting for whisper, so that READING never waits on TRANSCRIBING.

    Read in step with transcription — the shape this route had first — the reader stalls
    for the whole of every whisper call, the sidecar's queue fills behind it, and the
    captioner ends up working through audio that was on the air a minute ago. Because it
    never catches up, that lag is permanent: captions arrive long after the listener has
    heard the words, which is the one failure the client cannot correct for.

    What waits here is transcribed TOGETHER rather than one clip at a time. Whisper's
    cost on this box is flat in clip length (~10.7 s for 4 s of audio and for 11 s
    alike), so a merged clip costs what a single one does and loses no words — where
    taking only the newest would silently drop whole sentences. The cap is what keeps a
    merge from reaching back further than a live caption sensibly can.
    """

    def __init__(self, max_seconds: float = CAPTION_BACKLOG_S) -> None:
        self._max = max_seconds
        self._held: list[tuple[float, bytes, float]] = []

    def add(self, started: float, wav: bytes) -> None:
        self._held.append((started, wav, clip_seconds(wav)))
        # Give up the OLDEST past the cap. Dropping the newest instead would leave the
        # captioner reading history while the live edge went by unseen.
        while len(self._held) > 1 and sum(c[2] for c in self._held) > self._max:
            self._held.pop(0)

    def take(self) -> tuple[float, bytes] | None:
        """Everything waiting, as one clip stamped with the first segment's start."""
        if not self._held:
            return None
        held, self._held = self._held, []
        if len(held) == 1:
            return held[0][0], held[0][1]
        return held[0][0], merge([wav for _, wav, _ in held])
