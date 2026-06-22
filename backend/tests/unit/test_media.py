"""Video frame sampling (jbrain.media): probe, even-spaced downscaled extraction,
millisecond stamps, and near-duplicate dedup. Synthetic clips are generated with
ffmpeg so the suite needs no fixture binaries; the whole module skips when ffmpeg
isn't on PATH (it is in CI and the worker image)."""

import io
import subprocess

import pytest
from PIL import Image

from jbrain.media import (
    DEFAULT_LONGEST_EDGE,
    SampledFrame,
    ffmpeg_available,
    probe_duration_s,
    sample_frames,
)

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


def test_probe_duration_reads_seconds(tmp_path) -> None:
    duration = probe_duration_s(tmp_path / "clip.mp4")  # missing file → None
    assert duration is None
    (tmp_path / "clip.mp4").write_bytes(_make_clip(tmp_path, seconds=4))
    assert probe_duration_s(tmp_path / "clip.mp4") == pytest.approx(4.0, abs=0.3)


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


def test_samples_evenly_spaced_downscaled_frames(tmp_path) -> None:
    # dedup off isolates pure extraction: fps=8/8s=1 → ~8 frames at 1s spacing.
    clip = _make_clip(tmp_path, seconds=8, size="640x480", rate=15)
    frames = sample_frames(clip, max_frames=8, dedup_distance=0)

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


def test_frame_budget_is_capped(tmp_path) -> None:
    # A long clip still yields at most max_frames (cost stays flat with length).
    clip = _make_clip(tmp_path, seconds=30, rate=15)
    frames = sample_frames(clip, max_frames=6, dedup_distance=0)
    assert 1 < len(frames) <= 6


def test_does_not_upscale_small_frames(tmp_path) -> None:
    clip = _make_clip(tmp_path, seconds=3, size="160x120", rate=10)
    frames = sample_frames(clip, max_frames=4, longest_edge=768, dedup_distance=0)
    with Image.open(io.BytesIO(frames[0].jpeg)) as img:
        assert max(img.size) <= 160  # never blown up past the source


def test_dedup_collapses_a_static_clip(tmp_path) -> None:
    # A flat-color clip is identical frame-to-frame → dedup keeps essentially one.
    frames = sample_frames(_make_static_clip(tmp_path, seconds=6), max_frames=12)
    assert len(frames) == 1


def test_dedup_keeps_distinct_frames(tmp_path) -> None:
    # A strongly-varying clip: dedup must NOT collapse genuinely different frames.
    frames = sample_frames(_make_varying_clip(tmp_path, seconds=8), max_frames=8)
    assert len(frames) > 2


def test_dedup_disabled_keeps_all_static_frames(tmp_path) -> None:
    frames = sample_frames(_make_static_clip(tmp_path, seconds=6), max_frames=12, dedup_distance=0)
    assert len(frames) > 1  # without dedup the identical frames are all kept


def test_garbage_bytes_yield_no_frames() -> None:
    assert sample_frames(b"not a video at all") == []
