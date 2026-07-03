"""Video frame sampling for `analyze_video` (docs/archive/VIDEO_ANALYSIS_PLAN.md).

The video sibling of `jbrain.transcribe`: turn a video's bytes into a short,
bounded set of still frames to hand the vision LLM, plus a probe of the clip's
duration. We shell out to the system **ffmpeg/ffprobe** (added to the worker image
and `scripts/dev-setup.sh`) rather than pull a heavyweight decode library — ffmpeg
does the decoding/scaling, and Pillow only computes the cheap dedup hash.

Sampling (owner decision, docs/archive/VIDEO_ANALYSIS_PLAN.md): K evenly-spaced frames
(≈ every 100/K percent of the clip), capped at `max_frames`, each downscaled so its
longest edge is `longest_edge`, then near-duplicate frames are dropped by a
perceptual (difference) hash so a static stretch doesn't spend the budget on
identical stills. Each kept frame carries its millisecond offset into the clip so a
later stage can align it to the transcript timeline.

Nothing here calls the LLM or touches storage — it is pure media work, run off the
event loop by the caller (a worker job).
"""

import io
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog
from PIL import Image

log = structlog.get_logger()

# Owner-chosen defaults (docs/archive/VIDEO_ANALYSIS_PLAN.md): a bounded frame budget keeps
# the VLM cost flat regardless of clip length; 768px is the VLM-friendly longest
# edge from the research; the hash distance is the near-duplicate threshold.
DEFAULT_MAX_FRAMES = 24
DEFAULT_LONGEST_EDGE = 768
# Two 64-bit dHashes within this Hamming distance are "the same shot" → drop the
# later one. 6/64 tolerates compression noise/tiny motion without dropping real cuts.
DEFAULT_DEDUP_DISTANCE = 6
# A hard ceiling on the probed duration we trust (a corrupt header can report
# absurd values); clamps the per-frame timestamp math.
_MAX_REASONABLE_DURATION_S = 24 * 60 * 60


@dataclass(frozen=True)
class SampledFrame:
    """One sampled still: its offset into the clip and the JPEG bytes (downscaled)."""

    timestamp_ms: int
    jpeg: bytes


def ffmpeg_available() -> bool:
    """Whether both ffmpeg and ffprobe are on PATH. The feature degrades gracefully
    when they aren't (the tool/job is simply not offered), like the whisper gate."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_duration_s(path: Path) -> float | None:
    """The clip's duration in seconds via ffprobe, or None if it can't be read
    (a streamed container without a header duration; the caller falls back to a
    fixed sampling rate)."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.info("video.probe_failed", error=str(exc))
        return None
    try:
        seconds = float(out)
    except ValueError:
        return None
    if seconds <= 0 or seconds > _MAX_REASONABLE_DURATION_S:
        return None
    return seconds


def sample_frames(
    video: bytes,
    *,
    max_frames: int = DEFAULT_MAX_FRAMES,
    longest_edge: int = DEFAULT_LONGEST_EDGE,
    dedup_distance: int = DEFAULT_DEDUP_DISTANCE,
) -> list[SampledFrame]:
    """Sample up to `max_frames` evenly-spaced, downscaled, deduped JPEG frames.

    The bytes are written to a temp file (ffmpeg/ffprobe need a seekable input to
    read the duration and seek evenly). With a known duration the sampler asks
    ffmpeg for `fps = max_frames / duration` so the kept frames are one-per-bucket;
    without it (no header duration) it falls back to 1 fps capped at `max_frames`.
    Near-duplicate frames are then dropped by dHash. Returns [] when ffmpeg is
    unavailable or the clip yields no decodable frames (never raises for empty)."""
    if not ffmpeg_available():
        log.info("video.sample_skipped", reason="ffmpeg unavailable")
        return []
    with tempfile.TemporaryDirectory(prefix="jbrain-vid-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "in"
        src.write_bytes(video)
        duration = probe_duration_s(src)
        fps = _sampling_fps(duration, max_frames)
        frames = _extract(src, tmpdir, fps=fps, longest_edge=longest_edge, max_frames=max_frames)
        if not frames:
            return []
        stamped = _stamp(frames, duration=duration, fps=fps)
        return _dedup(stamped, distance=dedup_distance)


def _sampling_fps(duration: float | None, max_frames: int) -> float:
    """Frames/second so the whole clip yields ≈ `max_frames`. Unknown duration →
    1 fps (the `-frames:v` cap still bounds the count). Floored so a very long clip
    doesn't ask ffmpeg for an absurdly tiny, rounding-fragile rate."""
    if duration is None or duration <= 0:
        return 1.0
    return max(max_frames / duration, 0.05)


def _extract(
    src: Path, tmpdir: Path, *, fps: float, longest_edge: int, max_frames: int
) -> list[Path]:
    """Run ffmpeg to write ≤ max_frames downscaled JPEGs; return their sorted paths.
    `scale='min(LE,iw)':-2` never upscales and keeps an even height (codec-safe)."""
    pattern = tmpdir / "frame_%05d.jpg"
    vf = f"fps={fps:.6f},scale='min({longest_edge},iw)':-2"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(src),
                "-vf",
                vf,
                "-vsync",
                "vfr",
                "-frames:v",
                str(max_frames),
                "-q:v",
                "3",
                str(pattern),
            ],
            capture_output=True,
            timeout=600,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("video.extract_failed", stderr=exc.stderr.decode("utf-8", "replace")[-500:])
        return []
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("video.extract_failed", error=str(exc))
        return []
    return sorted(tmpdir.glob("frame_*.jpg"))


def _stamp(paths: list[Path], *, duration: float | None, fps: float) -> list[SampledFrame]:
    """Attach each kept frame's offset. With the fps filter, output frame i sits at
    ≈ i / fps seconds; when the duration is known we clamp the last frame to it."""
    out: list[SampledFrame] = []
    for i, p in enumerate(paths):
        seconds = i / fps if fps > 0 else 0.0
        if duration is not None:
            seconds = min(seconds, duration)
        out.append(SampledFrame(timestamp_ms=int(seconds * 1000), jpeg=p.read_bytes()))
    return out


def _dedup(frames: list[SampledFrame], *, distance: int) -> list[SampledFrame]:
    """Drop frames whose dHash is within `distance` of the last KEPT frame — a static
    stretch collapses to one still while real cuts survive. The first frame is always
    kept. `distance <= 0` disables dedup."""
    if distance <= 0 or len(frames) < 2:
        return frames
    kept: list[SampledFrame] = []
    last_hash: int | None = None
    for frame in frames:
        h = _dhash(frame.jpeg)
        if h is None:  # unreadable frame — keep it rather than guess
            kept.append(frame)
            last_hash = None
            continue
        if last_hash is not None and _hamming(h, last_hash) <= distance:
            continue
        kept.append(frame)
        last_hash = h
    return kept


def _dhash(jpeg: bytes, *, size: int = 8) -> int | None:
    """A 64-bit difference hash: grayscale → (size+1)×size, then each pixel-vs-its-
    right-neighbor comparison is one bit. Robust to scaling/compression, cheap."""
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            small = img.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
            px = small.tobytes()  # one byte per pixel in "L" mode, row-major
    except (OSError, ValueError):
        return None
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] > px[base + col + 1])
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def total_jpeg_bytes(frames: Iterable[SampledFrame]) -> int:
    """Sum of the kept frames' JPEG sizes — for the caller's budget logging."""
    return sum(len(f.jpeg) for f in frames)
