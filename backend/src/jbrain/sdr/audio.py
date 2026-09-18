"""The two things done to a recording's audio: measure it, and cut it.

Both are ffmpeg (already in the api image — `backend/Dockerfile`), run through
`jbrain.media.run_media_proc` so a wedged decode is bounded and a cancelled request
never leaves an orphan behind. Kept apart from `sdr/recordings.py` (rows) and
`sdr/recorder.py` (the live stream) because this module knows nothing about either: it
takes a path and gives back numbers, which is also what makes it testable without a
database or a radio.

**Nothing here may cost a recording.** Every failure returns empty rather than raising:
a clip whose waveform could not be computed is still a clip, and the library must show
it. The trim is the one exception — a cut that failed must not repoint the row — and it
says so by returning a `Cut` the route turns into a refusal.

**A cut is not finished until it has been proven to play.** `ffmpeg -c copy` exits 0 and
writes a ~621-byte header-only file when the seek lands past the last frame, so "the
process succeeded" and "there is audio in it" are different facts. `cut_clip` measures
its own output before handing it back, in the temp directory, BEFORE any of it reaches
the store — so an unplayable cut can neither be stored nor be mistaken for a length.
"""

from __future__ import annotations

import array
import asyncio
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from jbrain.media import run_media_proc

log = structlog.get_logger(__name__)

#: How many buckets the level envelope is reduced to, whatever the clip's length.
#:
#: The trim sheet draws 116 bars at phone width (`docs/mocks/recording/d-trim-sheet.html`,
#: `N = 116`). 400 is roughly three times that, so a wider screen — or a client that
#: redraws the sheet zoomed — has real data to draw rather than a stretched 116, while
#: three-decimal floats keep the stored jsonb around 2 kB no matter how long the
#: recording is. A FIXED count (not "one bucket per second") is what lets the client draw
#: the waveform without knowing the duration first.
PEAK_BUCKETS = 400

#: Decode rate for the envelope, in Hz. 4 kHz mono s16le is 8 kB/s — the same rate as the
#: sidecar's 64 kbps MP3 — so the decoded audio buffered to compute peaks is never larger
#: than the blob it came from. An envelope needs the loudness of a window, not the
#: waveform inside it, so the discarded high band costs the picture nothing.
PEAK_RATE_HZ = 4_000

#: Full-scale for signed 16-bit, so peaks come back as 0..1 and the client scales them to
#: whatever height it draws.
_FULL_SCALE = 32_768.0

#: Bounds. A decode reads the whole clip and a copy-cut barely reads it at all, so these
#: are generous for anything the box can hold and still finite — a wedged ffmpeg must
#: surface as "no waveform", never as a request that never answers.
DECODE_TIMEOUT_S = 120.0
CUT_TIMEOUT_S = 60.0


async def levels(path: Path) -> tuple[list[float], float | None]:
    """`(peaks, duration_s)` for a stored clip — `([], None)` if ffmpeg could not read it.

    The duration is counted from the decoded samples rather than read out of the MP3
    header, because it is the number the trim sheet's handles are placed against: a
    header's claim and what the file actually plays can differ, and after a `-c copy` cut
    it is the sample count that is true.
    """
    pcm = await _decode(path)
    if not pcm:
        return [], None
    samples = array.array("h")
    # ffmpeg was asked for s16le; `array` is native order, so swap on the rare big-endian
    # host rather than computing an envelope out of byte-swapped noise.
    samples.frombytes(pcm[: len(pcm) - len(pcm) % samples.itemsize])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return [], None
    duration_s = len(samples) / PEAK_RATE_HZ
    return await asyncio.to_thread(_envelope, samples), duration_s


def _envelope(samples: array.array) -> list[float]:
    """Peak magnitude per bucket, 0..1.

    Peak rather than RMS: this picture exists so the owner can see where the silence
    ends, and RMS flattens exactly the transients that mark the start of a transmission.
    `max`/`min` over a slice run in C, so an hour of audio costs milliseconds — a Python
    loop over 14 million samples would not.
    """
    total = len(samples)
    out: list[float] = []
    for i in range(PEAK_BUCKETS):
        start = total * i // PEAK_BUCKETS
        end = max(start + 1, total * (i + 1) // PEAK_BUCKETS)
        window = samples[start:end]
        # -32768 has no positive counterpart, hence the min() half rather than abs().
        loudest = max(max(window), -min(window))
        out.append(round(min(1.0, loudest / _FULL_SCALE), 3))
    return out


async def _decode(path: Path) -> bytes:
    """Raw mono PCM for the whole clip, or empty when ffmpeg cannot oblige."""
    try:
        rc, stdout, stderr = await run_media_proc(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                str(PEAK_RATE_HZ),
                "-",
            ],
            timeout_s=DECODE_TIMEOUT_S,
        )
    except (OSError, TimeoutError) as exc:
        log.warning("sdr_audio.decode_failed", error=repr(exc))
        return b""
    if rc != 0:
        log.warning("sdr_audio.decode_rc", rc=rc, stderr=stderr[:200].decode("utf-8", "replace"))
        # ffmpeg writes what it managed before failing; a truncated clip still has a
        # waveform, and half a picture beats none.
        return stdout
    return stdout


@dataclass(frozen=True)
class Cut:
    """What a copy-cut produced, and the proof that it contains audio.

    Three states, and the caller must tell them apart because two of them are refusals
    and only one of them may repoint a row:

    * `data` empty — ffmpeg did not run, or failed. Nothing was cut.
    * `data` present, `duration_s` None — ffmpeg exited 0 and wrote a file with no
      decodable frames. This is the state that destroys a recording if it is trusted: a
      seek past the end of the audio produces a header and nothing else.
    * `data` present, `duration_s` set — a real clip, of exactly this measured length.
    """

    data: bytes = b""
    peaks: list[float] = field(default_factory=list)
    duration_s: float | None = None

    @property
    def plays(self) -> bool:
        return bool(self.data) and self.duration_s is not None


async def cut_clip(source: Path, start_s: float, end_s: float) -> Cut:
    """The clip between two offsets, measured — see `Cut` for the three ways this ends.

    `-c copy`, so the frames are copied rather than re-encoded: lossless and instant, at
    the price of landing on a frame boundary (1152 samples = 72 ms at the sidecar's
    16 kHz). That is why the sheet offers a per-frame nudge instead of implying
    millisecond precision, and why the length comes back MEASURED rather than echoed.

    **Both `-ss` and `-to` go BEFORE `-i`**, which makes them input options measured on
    the input's own timeline. Moved after `-i` (the more familiar spelling) `-to` becomes
    an output option relative to the seek point, and every trim would cut `end_s`
    seconds of audio starting at `start_s` — a longer clip than was asked for, silently.

    **The result is measured here, not by the caller.** ffmpeg exits 0 for a seek that
    lands past the last frame and writes a header-only file, so a caller that treated
    non-empty output as success would store 621 bytes, record the length it ASKED for,
    and then delete the original — 200 OK, audio gone. Measuring in the temp directory
    means an unplayable cut never reaches the store at all, so there is nothing to
    orphan and nothing to clean up.
    """
    with tempfile.TemporaryDirectory(prefix="sdr-trim-") as tmp:
        out = Path(tmp) / "cut.mp3"
        try:
            rc, _, stderr = await run_media_proc(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    f"{start_s:.3f}",
                    "-to",
                    f"{end_s:.3f}",
                    "-i",
                    str(source),
                    "-c",
                    "copy",
                    "-f",
                    "mp3",
                    "-y",
                    str(out),
                ],
                timeout_s=CUT_TIMEOUT_S,
            )
        except (OSError, TimeoutError) as exc:
            log.warning("sdr_audio.cut_failed", error=repr(exc))
            return Cut()
        if rc != 0 or not out.exists():
            log.warning("sdr_audio.cut_rc", rc=rc, stderr=stderr[:200].decode("utf-8", "replace"))
            return Cut()
        data = await asyncio.to_thread(out.read_bytes)
        if not data:
            return Cut()
        peaks, measured = await levels(out)
        if measured is None:
            log.warning("sdr_audio.cut_has_no_audio", bytes=len(data), start_s=start_s)
        return Cut(data=data, peaks=peaks, duration_s=measured)
