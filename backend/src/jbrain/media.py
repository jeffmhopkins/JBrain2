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

import asyncio
import io
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import structlog
from PIL import Image

log = structlog.get_logger()


async def run_media_proc(cmd: list[str], *, timeout_s: float) -> tuple[int | None, bytes, bytes]:
    """Run one media subprocess (ffmpeg/ffprobe) on the event loop, bounded by `timeout_s`
    and **cancel-safe**: a timeout OR a cancelled turn/job `kill()`s the child promptly
    and reaps it, so no ffmpeg orphan outlives the work. This is the property the old
    blocking `subprocess.run` in a thread could not offer — a thread can't be cancelled,
    so a Stop left ffmpeg running to its own timeout (DEFERRED_TOOL_CALLS_PLAN.md P1).
    Every command is still an argv-list, never a shell string (the URL/path is data).
    Returns `(returncode, stdout, stderr)`; raises `OSError` on a spawn failure and
    `TimeoutError` on the wall-clock bound (both a clean "no frames" for the caller)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout, stderr


def _sorted_jpegs(tmpdir: Path, pattern: str = "frame_*.jpg") -> list[Path]:
    """The sampled JPEG output paths in order — a sync helper so the async samplers
    keep their (blocking) directory glob off an `async def` body (ruff ASYNC240)."""
    return sorted(tmpdir.glob(pattern))


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


async def probe_duration_s(path: Path) -> float | None:
    """The clip's duration in seconds via ffprobe, or None if it can't be read
    (a streamed container without a header duration; the caller falls back to a
    fixed sampling rate)."""
    try:
        rc, stdout, _ = await run_media_proc(
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
            timeout_s=60,
        )
    except (OSError, TimeoutError) as exc:
        log.info("video.probe_failed", error=str(exc))
        return None
    if rc != 0:
        return None
    try:
        seconds = float(stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return None
    if seconds <= 0 or seconds > _MAX_REASONABLE_DURATION_S:
        return None
    return seconds


async def sample_frames(
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
    unavailable or the clip yields no decodable frames (never raises for empty).

    Async so the ffmpeg/ffprobe legs run as cancel-safe subprocesses on the event
    loop — a cancelled turn or job kills them promptly (DEFERRED_TOOL_CALLS_PLAN.md
    P1), where the old thread-offloaded blocking call left ffmpeg orphaned."""
    if not ffmpeg_available():
        log.info("video.sample_skipped", reason="ffmpeg unavailable")
        return []
    with tempfile.TemporaryDirectory(prefix="jbrain-vid-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "in"
        src.write_bytes(video)
        duration = await probe_duration_s(src)
        fps = _sampling_fps(duration, max_frames)
        frames = await _extract(
            src, tmpdir, fps=fps, longest_edge=longest_edge, max_frames=max_frames
        )
        if not frames:
            return []
        stamped = _stamp(frames, duration=duration, fps=fps)
        return dedup_frames(stamped, distance=dedup_distance)


def _sampling_fps(duration: float | None, max_frames: int) -> float:
    """Frames/second so the whole clip yields ≈ `max_frames`. Unknown duration →
    1 fps (the `-frames:v` cap still bounds the count). Floored so a very long clip
    doesn't ask ffmpeg for an absurdly tiny, rounding-fragile rate."""
    if duration is None or duration <= 0:
        return 1.0
    return max(max_frames / duration, 0.05)


async def _extract(
    src: Path, tmpdir: Path, *, fps: float, longest_edge: int, max_frames: int
) -> list[Path]:
    """Run ffmpeg to write ≤ max_frames downscaled JPEGs; return their sorted paths.
    `scale='min(LE,iw)':-2` never upscales and keeps an even height (codec-safe)."""
    pattern = tmpdir / "frame_%05d.jpg"
    vf = f"fps={fps:.6f},scale='min({longest_edge},iw)':-2"
    try:
        rc, _, stderr = await run_media_proc(
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
            timeout_s=600,
        )
    except (OSError, TimeoutError) as exc:
        log.warning("video.extract_failed", error=str(exc))
        return []
    if rc != 0:
        log.warning("video.extract_failed", stderr=stderr.decode("utf-8", "replace")[-500:])
        return []
    return _sorted_jpegs(tmpdir)


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


def dedup_frames(
    frames: list[SampledFrame], *, distance: int = DEFAULT_DEDUP_DISTANCE
) -> list[SampledFrame]:
    """Drop frames whose dHash is within `distance` of the last KEPT frame — a static
    stretch collapses to one still while real cuts survive. The first frame is always
    kept. `distance <= 0` disables dedup. Public so the URL-sourced stream sampler
    (jbrain.stream) reuses the exact same perceptual dedup as the attachment path."""
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


# A filmstrip cell is small, so a card thumbnail needs far less than the VLM-facing
# 768px frame; 320px keeps an inline data-URI a few KB rather than tens.
DEFAULT_THUMB_EDGE = 320


def jpeg_thumbnail(jpeg: bytes, *, max_edge: int = DEFAULT_THUMB_EDGE, quality: int = 70) -> bytes:
    """A smaller re-encoded JPEG for display — downscale so the longest edge is
    `max_edge` (never upscale). Used to inline a card thumbnail as a compact data URI
    for a source with no served thumbnail route (a stream frame). Returns the input
    unchanged if it can't be decoded (the caller still gets *some* bytes)."""
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
    except (OSError, ValueError):
        return jpeg
