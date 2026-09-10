"""`jbrain.sdr.audio` — the level envelope and the copy-cut.

ffmpeg is scripted rather than run, for two reasons: the argv is itself the thing worth
asserting (`-ss`/`-to` on the wrong side of `-i` silently cuts a longer clip than was
asked for), and the failure paths — which must cost a waveform and never a recording —
cannot be provoked on demand with a real decoder.

The case worth naming: **a copy-cut can succeed and produce no audio.** Seeking past the
last frame exits 0 and writes a ~621-byte header, so `rc == 0` and "there is a clip" are
different facts, and a trim that conflated them deleted the original in exchange for
silence. That is why `cut_clip` runs a second, measuring pass over its own output, and
why the fake here answers a decode differently from a cut.
"""

from __future__ import annotations

import array
import struct
from pathlib import Path
from typing import Any

import pytest

from jbrain.sdr import audio


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _write(path: str, data: bytes) -> None:
    """A sync helper so the fake subprocess keeps its file write off an `async def`
    body (ruff ASYNC240) — the same reason `media._sorted_jpegs` exists."""
    Path(path).write_bytes(data)


def _is_decode(cmd: list[str]) -> bool:
    """A decode-to-PCM rather than a copy-cut. `cut_clip` runs BOTH — it measures its own
    output before returning it — so a fake that answered them the same way could not tell
    "the cut wrote a file" from "the file has audio in it", which is the whole bug."""
    return "s16le" in cmd


def _script(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    rc: int | None = 0,
    boom: Exception | None = None,
    writes: bytes | None = None,
    decoded: bytes | None = None,
) -> list[list[str]]:
    """Answer every media subprocess with this, recording the argv it was given.

    `decoded` is what the cut's own measuring pass decodes back out of the file it wrote:
    PCM for a clip that plays, and the default (nothing) for the header-only file ffmpeg
    writes when the seek lands past the last frame.
    """
    seen: list[list[str]] = []

    async def fake(cmd: list[str], *, timeout_s: float) -> tuple[int | None, bytes, bytes]:
        seen.append(cmd)
        if boom is not None:
            raise boom
        if _is_decode(cmd):
            # `decoded` overrides for the cut's measuring pass; without it a decode falls
            # through to `stdout`/`rc`, which is what the `levels`-only tests script.
            return (0, decoded, b"") if decoded is not None else (rc, stdout, b"ffmpeg said no")
        if writes is not None:
            _write(cmd[-1], writes)
        return rc, stdout, b"ffmpeg said no"

    monkeypatch.setattr(audio, "run_media_proc", fake)
    return seen


def test_the_envelope_is_a_fixed_number_of_peak_readings() -> None:
    """A FIXED bucket count is what lets the client draw the waveform without knowing
    the clip's duration, and peak (not RMS) is what keeps the start of a transmission
    visible against the silence before it."""
    quiet = [100] * 1000
    loud = [0, 16_384] * 500
    samples = array.array("h", quiet + loud)

    envelope = audio._envelope(samples)

    assert len(envelope) == audio.PEAK_BUCKETS
    assert all(0.0 <= v <= 1.0 for v in envelope)
    assert envelope[0] == pytest.approx(100 / 32_768, abs=0.001)
    assert envelope[-1] == pytest.approx(0.5, abs=0.001)


def test_the_most_negative_sample_is_still_a_peak() -> None:
    """-32768 has no positive counterpart, so `abs()` on it overflows the scale; the
    envelope reads magnitude, and a full-scale negative excursion is full scale."""
    envelope = audio._envelope(array.array("h", [-32_768] * audio.PEAK_BUCKETS))

    assert envelope == [1.0] * audio.PEAK_BUCKETS


async def test_levels_counts_the_duration_out_of_the_decoded_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The number the trim sheet places its handles against comes from the audio, not
    from a header a `-c copy` cut may have left saying something else."""
    seconds = 3
    _script(monkeypatch, stdout=_pcm([1234] * (audio.PEAK_RATE_HZ * seconds)))

    peaks, duration_s = await audio.levels(tmp_path / "clip.mp3")

    assert duration_s == pytest.approx(float(seconds))
    assert len(peaks) == audio.PEAK_BUCKETS


async def test_a_decoder_that_fails_costs_a_waveform_not_a_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`([], None)`, never an exception: the caller writes the row either way, and an
    empty envelope means "could not read it" rather than "silence"."""
    _script(monkeypatch, rc=1, stdout=b"")

    assert await audio.levels(tmp_path / "clip.mp3") == ([], None)


async def test_a_decoder_that_never_returns_costs_a_waveform_not_a_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _script(monkeypatch, boom=TimeoutError("wedged"))

    assert await audio.levels(tmp_path / "clip.mp3") == ([], None)

    _script(monkeypatch, boom=OSError("no ffmpeg on this box"))
    assert await audio.levels(tmp_path / "clip.mp3") == ([], None)


async def test_the_cut_seeks_on_the_input_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Both `-ss` and `-to` before `-i`.**

    After `-i`, `-to` becomes an output option relative to the seek point, so every trim
    would keep `end_s` seconds STARTING at `start_s` — a longer clip than was asked for,
    with nothing anywhere saying so. `-c copy` matters just as much: re-encoding a radio
    clip on this box is neither lossless nor instant."""
    seen = _script(monkeypatch, writes=b"cut-bytes", decoded=_pcm([1000] * 36_000))

    out = await audio.cut_clip(tmp_path / "clip.mp3", 4.0, 13.0)

    assert out.data == b"cut-bytes"
    # Measured out of the result, not echoed back: 36000 samples at 4 kHz is 9 s.
    assert out.duration_s == pytest.approx(9.0)
    assert out.plays and len(out.peaks) == audio.PEAK_BUCKETS
    cut = next(cmd for cmd in seen if not _is_decode(cmd))
    assert cut.index("-ss") < cut.index("-to") < cut.index("-i")
    assert cut[cut.index("-ss") + 1] == "4.000"
    assert cut[cut.index("-to") + 1] == "13.000"
    assert cut[cut.index("-i") + 1] == str(tmp_path / "clip.mp3")
    assert cut[cut.index("-c") + 1] == "copy"


async def test_a_cut_with_no_frames_in_it_is_not_a_clip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**The one that costs a recording.** `ffmpeg -ss <past the audio> -c copy` exits 0
    and writes a ~621-byte header with nothing under it. Treating non-empty output as
    success is what let a trim repoint a row at silence, record the length it ASKED for,
    and then delete the original: 200 OK, audio gone.

    So the cut measures itself, in the temp directory, before anyone can store it — and
    says so by carrying no duration, which the route turns into a refusal."""
    _script(monkeypatch, rc=0, writes=b"ID3" + b"\x00" * 618)

    out = await audio.cut_clip(tmp_path / "clip.mp3", 4.99, 5.19)

    assert out.data  # ffmpeg "succeeded" and there ARE bytes...
    assert out.duration_s is None  # ...and not one frame of audio in them
    assert not out.plays


async def test_a_failed_cut_returns_nothing_rather_than_half_a_clip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The caller repoints the row on the strength of these bytes, so a failure has to
    be empty rather than partial."""
    _script(monkeypatch, rc=1, writes=b"half")

    out = await audio.cut_clip(tmp_path / "clip.mp3", 1.0, 2.0)

    assert out.data == b"" and not out.plays


async def test_a_cut_that_wrote_no_file_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rc 0 and no output happens — a seek past the end of the clip. Reading the file
    anyway would raise inside the route instead of refusing cleanly."""
    _script(monkeypatch, rc=0, writes=None)

    assert await audio.cut_clip(tmp_path / "clip.mp3", 1.0, 2.0) == audio.Cut()


async def test_a_cut_that_could_not_be_run_at_all_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A box without ffmpeg, or a wedged one. Distinct from "no frames": nothing ran, so
    the route says the trim did not happen rather than blaming the owner's selection."""
    _script(monkeypatch, boom=TimeoutError("wedged"))
    assert await audio.cut_clip(tmp_path / "clip.mp3", 1.0, 2.0) == audio.Cut()

    _script(monkeypatch, boom=OSError("no ffmpeg on this box"))
    assert await audio.cut_clip(tmp_path / "clip.mp3", 1.0, 2.0) == audio.Cut()


async def test_the_cut_leaves_no_temporary_file_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    holder: list[Any] = []

    async def fake(cmd: list[str], *, timeout_s: float) -> tuple[int | None, bytes, bytes]:
        if _is_decode(cmd):
            return 0, _pcm([1] * 4_000), b""
        holder.append(Path(cmd[-1]).parent)
        _write(cmd[-1], b"x")
        return 0, b"", b""

    monkeypatch.setattr(audio, "run_media_proc", fake)
    await audio.cut_clip(tmp_path / "clip.mp3", 1.0, 2.0)

    assert not holder[0].exists()
