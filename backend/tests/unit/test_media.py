"""Video frame sampling (jbrain.media): probe, even-spaced downscaled extraction,
millisecond stamps, and near-duplicate dedup. Synthetic clips are generated with
ffmpeg so the suite needs no fixture binaries; the whole module skips when ffmpeg
isn't on PATH (it is in CI and the worker image)."""

import asyncio
import io
import subprocess
import time
from unittest import mock

import pytest
from PIL import Image

from jbrain.media import (
    DEFAULT_LONGEST_EDGE,
    SampledFrame,
    ffmpeg_available,
    probe_duration_s,
    run_media_proc,
    sample_frames,
)

# A real ffmpeg that runs at native rate for ~30 wall-clock seconds (`-re`), so a
# timeout/cancel has a live child to kill — the P1 cancel-safety proof below.
_LONG_FFMPEG = ["ffmpeg", "-re", "-f", "lavfi", "-i", "testsrc=duration=30", "-f", "null", "-"]

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe not installed")


def _make_clip(tmp_path, *, seconds: int = 5, size: str = "320x240", rate: int = 15) -> bytes:
    """A synthetic test clip: ffmpeg's `testsrc` is a moving pattern, so frames
    differ over time (dedup keeps them)."""
    out = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size={size}:rate={rate}",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out.read_bytes()


def _make_static_clip(tmp_path, *, seconds: int = 6) -> bytes:
    """A single flat color for the whole clip — every frame is identical, so dedup
    should collapse it to one."""
    out = tmp_path / "static.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=teal:size=320x240:duration={seconds}:rate=15",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out.read_bytes()


async def test_probe_duration_reads_seconds(tmp_path) -> None:
    duration = await probe_duration_s(tmp_path / "clip.mp4")  # missing file → None
    assert duration is None
    (tmp_path / "clip.mp4").write_bytes(_make_clip(tmp_path, seconds=4))
    assert await probe_duration_s(tmp_path / "clip.mp4") == pytest.approx(4.0, abs=0.3)


def _make_varying_clip(tmp_path, *, seconds: int = 8, rate: int = 15) -> bytes:
    """A strongly-animating clip (mandelbrot zoom) — consecutive sampled frames
    differ a lot, so dedup keeps them."""
    out = tmp_path / "vary.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"mandelbrot=size=320x240:rate={rate}",
            "-t",
            str(seconds),
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out.read_bytes()


async def test_samples_evenly_spaced_downscaled_frames(tmp_path) -> None:
    # dedup off isolates pure extraction: fps=8/8s=1 → ~8 frames at 1s spacing.
    clip = _make_clip(tmp_path, seconds=8, size="640x480", rate=15)
    frames = await sample_frames(clip, max_frames=8, dedup_distance=0)

    assert 1 < len(frames) <= 8
    assert all(isinstance(f, SampledFrame) for f in frames)
    # Timestamps are non-decreasing, start near 0, and stay within the clip.
    stamps = [f.timestamp_ms for f in frames]
    assert stamps == sorted(stamps)
    assert stamps[0] < 1000 and stamps[-1] <= 8_000
    # Each frame is a real JPEG, downscaled so the longest edge ≤ the cap.
    for f in frames:
        with Image.open(io.BytesIO(f.jpeg)) as img:
            assert img.format == "JPEG"
            assert max(img.size) <= DEFAULT_LONGEST_EDGE


async def test_frame_budget_is_capped(tmp_path) -> None:
    # A long clip still yields at most max_frames (cost stays flat with length).
    clip = _make_clip(tmp_path, seconds=30, rate=15)
    frames = await sample_frames(clip, max_frames=6, dedup_distance=0)
    assert 1 < len(frames) <= 6


async def test_does_not_upscale_small_frames(tmp_path) -> None:
    clip = _make_clip(tmp_path, seconds=3, size="160x120", rate=10)
    frames = await sample_frames(clip, max_frames=4, longest_edge=768, dedup_distance=0)
    with Image.open(io.BytesIO(frames[0].jpeg)) as img:
        assert max(img.size) <= 160  # never blown up past the source


async def test_dedup_collapses_a_static_clip(tmp_path) -> None:
    # A flat-color clip is identical frame-to-frame → dedup keeps essentially one.
    frames = await sample_frames(_make_static_clip(tmp_path, seconds=6), max_frames=12)
    assert len(frames) == 1


async def test_dedup_keeps_distinct_frames(tmp_path) -> None:
    # A strongly-varying clip: dedup must NOT collapse genuinely different frames.
    frames = await sample_frames(_make_varying_clip(tmp_path, seconds=8), max_frames=8)
    assert len(frames) > 2


async def test_dedup_disabled_keeps_all_static_frames(tmp_path) -> None:
    frames = await sample_frames(
        _make_static_clip(tmp_path, seconds=6), max_frames=12, dedup_distance=0
    )
    assert len(frames) > 1  # without dedup the identical frames are all kept


async def test_garbage_bytes_yield_no_frames() -> None:
    assert await sample_frames(b"not a video at all") == []


# --- P1 cancel-safety: a bounded/cancelled media subprocess is killed, not orphaned ---


async def test_run_media_proc_kills_child_on_timeout() -> None:
    # The child would run ~30s; a 0.5s bound must SIGKILL it and return promptly,
    # not block for the child's own duration (the old subprocess.run-in-a-thread bug).
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_media_proc(_LONG_FFMPEG, timeout_s=0.5)
    assert time.monotonic() - t0 < 10  # returned near the bound, not the 30s runtime


async def test_run_media_proc_kills_child_on_cancel() -> None:
    # A cancelled turn/job must terminate the ffmpeg child (no orphan running to its
    # own timeout) — the core reason P1 moves to create_subprocess_exec.
    spawned: list[asyncio.subprocess.Process] = []
    started = asyncio.Event()
    real = asyncio.create_subprocess_exec

    async def _spy(*args, **kwargs):
        proc = await real(*args, **kwargs)
        spawned.append(proc)
        started.set()
        return proc

    with mock.patch("asyncio.create_subprocess_exec", _spy):
        task = asyncio.create_task(run_media_proc(_LONG_FFMPEG, timeout_s=30))
        await started.wait()  # the child is now running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The helper reaped the child (returncode set) after killing it — not left running.
    assert spawned[0].returncode is not None
