"""URL-sourced video/stream sampling for the `analyze_stream` tool
(docs/archive/STREAM_ANALYSIS_PLAN.md, Wave 1).

The URL sibling of `jbrain.media` (which samples an attachment's bytes): resolve a
model-supplied video URL — a live stream or an on-demand video — to its direct
media URL with **yt-dlp**, then pull a bounded set of downscaled, deduped frames
(and optionally a short audio segment for whisper) with **ffmpeg**, WITHOUT ever
downloading the whole file. Two subprocess legs, both bounded:

  RESOLVE  yt-dlp turns a watch/live page URL into a direct media URL (an HLS
           manifest for a live stream, a progressive file for a VOD), plus the
           title / is_live / duration the tool needs. Run as the yt-dlp Python
           API off the event loop by the caller.
  SAMPLE   ffmpeg reads a bounded WINDOW of that media URL (`-ss`/`-t`) and writes
           ≤ N downscaled JPEG frames; an optional second ffmpeg run pulls the
           same window's audio as 16 kHz mono WAV for whisper. Frames reuse
           `jbrain.media`'s perceptual dedup so a static stretch collapses to one.

This is a **second sanctioned direct outbound leg for the jerv sandbox** (the
first is `web_fetch`), so it carries the same egress discipline (ASSISTANT.md,
invariant #9): the URL is untrusted, so the **resolved media host is run through
the shared SSRF guard** (`jbrain.web.fetch.guard_public_host`) before ffmpeg
opens it — a resolved private/loopback/link-local target is refused, so a crafted
URL can't turn ffmpeg into a read primitive against the box's own services. Every
subprocess is argv-list (never a shell string — the URL is data), bounded by a
wall-clock timeout, and yt-dlp is constrained to https, no playlist, no cookies,
no post-processors.

Pure media + subprocess work — no LLM, no DB, no storage. The caller runs it off
the event loop and hands the frames/audio to the shared caption→fuse→reduce core.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from jbrain.media import (
    DEFAULT_DEDUP_DISTANCE,
    DEFAULT_LONGEST_EDGE,
    SampledFrame,
    _sorted_jpegs,
    dedup_frames,
    ffmpeg_available,
    run_media_proc,
)
from jbrain.web.fetch import WebFetchError, guard_public_host

log = structlog.get_logger()

# Bounds (owner decisions, docs/archive/STREAM_ANALYSIS_PLAN.md). A live stream is
# unbounded, so every knob that could let ffmpeg read forever is capped here.
MAX_FRAMES = 24  # the analyze_video budget — flat VLM cost regardless of stream length
MAX_WINDOW_S = 120.0  # the longest slice we sample/transcribe in one call
DEFAULT_WINDOW_S = 10.0
DEFAULT_WINDOW_FRAMES = 8  # frames for a window-mode grab
DEFAULT_FULL_FRAMES = 16  # frames spread across a whole VOD in full mode
# In full (whole-VOD) mode we transcribe the entire audio track up to this length —
# generous enough for a typical video (GPU whisper handles ~30 min in a few minutes,
# which the "expensive" tool warns about). A longer clip (a podcast, an hour+ talk)
# still samples frames across the whole video but skips the transcript rather than
# tying up an in-turn whisper pass — download it as an attachment for the job-backed
# whole-video transcription instead.
MAX_FULL_AUDIO_S = 30 * 60.0
DEFAULT_MAX_HEIGHT = 720  # cap the resolved format so ffmpeg reads bounded bytes
AUDIO_SAMPLE_RATE = 16_000  # whisper's native rate; mono keeps the WAV small
_RESOLVE_TIMEOUT_S = 30
# ffmpeg gets the window plus generous slack for network + decode, then is killed —
# a slow-loris media host cannot hang the turn.
_FFMPEG_SLACK_S = 60
_RW_TIMEOUT_US = 20_000_000  # per-read socket timeout ffmpeg honours on a stalled host
# ffmpeg opens whatever a (possibly crafted) HLS/DASH manifest references. Restrict a
# URL input to network protocols only, so a malicious manifest cannot make ffmpeg open
# a `file:`, `pipe:`, `concat:`, `data:`, or `subfile:` target — the local-file /
# process exfil vectors. http stays (the residual same-family segment-host SSRF is
# bounded by the SSRF guard on the resolved host, the read timeouts, and the fact that
# no fetched-response bytes reach the model — only decoded pixels of valid video do).
_URL_PROTOCOLS = "https,http,tls,tcp,crypto,hls,httpproxy"

# Prefer a single combined (audio+video) format at or below the height cap — one URL
# ffmpeg reads for both frames and audio (YouTube VOD itag 22, live HLS variants).
# The non-strict `<=?` never fails the match; the final `/best` is the last resort.
_FORMAT = "best[height<=?{h}]/bestvideo[height<=?{h}]/best"


class StreamError(RuntimeError):
    """A stream URL could not be resolved or sampled — an unsupported/blocked URL,
    a resolved non-public target, or a media the tools couldn't read. Surfaced to
    the model as a recoverable tool error, never an unhandled exception."""


@dataclass(frozen=True)
class ResolvedStream:
    """What yt-dlp resolved a page URL into: the direct media URL ffmpeg reads, plus
    the metadata the tool needs to pick a sampling mode and label the card."""

    media_url: str
    title: str
    is_live: bool
    duration_s: float | None
    webpage_url: str
    # yt-dlp's extractor name (e.g. "youtube") and the source's own id — surfaced so
    # the card can embed the provider's player (YouTube today) synced to the analysis,
    # instead of only a still. Empty for a provider we don't embed.
    provider: str = ""
    video_id: str = ""
    # The HTTP headers yt-dlp used to fetch this format (User-Agent, etc.). ffmpeg MUST
    # send these or a signed googlevideo URL — issued for a specific player client like
    # ANDROID_VR — returns 403 Forbidden on the larger windowed/audio reads (a single
    # frame's tiny read often slips through, which is why single mode worked and window
    # didn't). Empty for a source that needs no special headers.
    http_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamSample:
    """The sampled product handed to the shared caption→fuse→reduce core: the
    downscaled deduped frames (window-relative timestamps) and, best-effort, the
    window's audio as WAV bytes for whisper (empty when the media had no audio track
    or audio wasn't requested)."""

    frames: list[SampledFrame]
    audio_wav: bytes = b""


# The resolve_stream shape, injectable so the analyze_stream handler can be tested
# without yt-dlp or a real network (a fake resolver returns a ResolvedStream pointing
# at a local clip).
Resolver = Callable[..., ResolvedStream]


def ytdlp_available() -> bool:
    """Whether yt-dlp can be imported. It is a normal backend dependency, so this is
    effectively always true in a synced env — but the tool gates on it the same way
    it gates on ffmpeg, so a stripped deployment degrades gracefully (the sidecar is
    dropped from the registry) rather than erroring at call time."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_stream(
    url: str, *, max_height: int = DEFAULT_MAX_HEIGHT, skip_guard: bool = False
) -> ResolvedStream:
    """Resolve a page/watch/live URL to a direct media URL with yt-dlp, then SSRF-guard
    the resolved host. Blocking (yt-dlp does network I/O) — the caller runs it off the
    event loop. `skip_guard` bypasses the host check for tests with no real network.
    Raises `StreamError` on an unresolvable URL or a non-public resolved target."""
    guard_public_host_or_stream(url, skip_dns=skip_guard)  # the INPUT URL must be public too
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - env without the dep
        raise StreamError("stream resolution is unavailable (yt-dlp not installed)") from exc

    opts: Any = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": _FORMAT.format(h=max_height),
        "socket_timeout": _RESOLVE_TIMEOUT_S,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises a wide, unstable set
        log.warning("stream.resolve_failed", error=repr(exc))
        raise StreamError("that URL couldn't be opened as a video stream") from exc

    resolved = _select_media(info, fallback_url=url)
    guard_public_host_or_stream(resolved.media_url, skip_dns=skip_guard)
    return resolved


def guard_public_host_or_stream(url: str, *, skip_dns: bool) -> None:
    """Apply the shared SSRF guard, translating its `WebFetchError` into a
    `StreamError` so callers handle one error type. Kept a named function so the
    guard is unit-testable without a real resolve."""
    try:
        guard_public_host(url, skip_dns=skip_dns)
    except WebFetchError as exc:
        raise StreamError(str(exc)) from exc


def _select_media(info: Any, *, fallback_url: str) -> ResolvedStream:
    """Turn yt-dlp's info dict into a `ResolvedStream`. With `noplaylist` a single
    video is expected, but a URL that still resolves to a playlist yields `entries`;
    take the first playable one. The direct media URL is the selected format's
    top-level `url`, or the first `requested_formats` entry (the video leg) when the
    selection merged separate A/V tracks. `info` is yt-dlp's untyped `_InfoDict`."""
    if not info:
        raise StreamError("that URL didn't resolve to any playable video")
    entries = info.get("entries")
    if entries:
        playable = [e for e in entries if e]
        if not playable:
            raise StreamError("that URL resolved to an empty playlist")
        info = playable[0]

    media_url = info.get("url")
    headers = info.get("http_headers") or {}
    if not media_url:
        formats = info.get("requested_formats") or []
        if formats and formats[0].get("url"):
            media_url = formats[0]["url"]
            # Prefer the selected format's own headers over the top-level ones.
            headers = formats[0].get("http_headers") or headers
    if not media_url:
        raise StreamError("that video had no directly-readable media URL")

    duration = info.get("duration")
    return ResolvedStream(
        media_url=str(media_url),
        title=str(info.get("title") or info.get("webpage_url") or fallback_url),
        is_live=bool(info.get("is_live")),
        duration_s=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        webpage_url=str(info.get("webpage_url") or fallback_url),
        provider=str(info.get("extractor") or "").lower(),
        video_id=str(info.get("id") or ""),
        http_headers={str(k): str(v) for k, v in dict(headers).items()},
    )


async def sample_stream(
    resolved: ResolvedStream,
    *,
    frames: int = 8,
    window_s: float = DEFAULT_WINDOW_S,
    seek_s: float = 0.0,
    want_audio: bool = False,
    longest_edge: int = DEFAULT_LONGEST_EDGE,
    dedup_distance: int = DEFAULT_DEDUP_DISTANCE,
) -> StreamSample:
    """Sample ≤ `frames` downscaled, deduped JPEG frames from a bounded window of the
    resolved media, plus (best-effort) that window's audio as WAV when `want_audio`.

    `frames <= 1` is the single-grab fast path (one still, no fps filter). For a VOD
    the window starts at `seek_s`; a live stream ignores `seek_s` and reads from the
    live edge. Timestamps are window-relative (ms from the window start) so frames and
    the transcript segment align on one timeline. Returns empty frames (never raises)
    when ffmpeg reads nothing decodable; audio is empty when the media had no audio
    track or the audio leg failed — the caller degrades to frames-only, exactly like
    the attachment path without whisper."""
    if not ffmpeg_available():
        log.info("stream.sample_skipped", reason="ffmpeg unavailable")
        return StreamSample(frames=[])

    frames = max(1, min(frames, MAX_FRAMES))
    window = max(0.0, min(window_s, MAX_WINDOW_S))
    seek = 0.0 if resolved.is_live else max(0.0, seek_s)

    with tempfile.TemporaryDirectory(prefix="jbrain-stream-") as tmp:
        tmpdir = Path(tmp)
        sampled = await _extract_frames(
            resolved.media_url,
            tmpdir,
            frames=frames,
            window=window,
            seek=seek,
            longest_edge=longest_edge,
            headers=resolved.http_headers,
        )
        deduped = dedup_frames(sampled, distance=dedup_distance)
        audio = b""
        if want_audio:
            audio = await _extract_audio(
                resolved.media_url, tmpdir, window=window, seek=seek, headers=resolved.http_headers
            )
    return StreamSample(frames=deduped, audio_wav=audio)


async def sample_stream_full(
    resolved: ResolvedStream,
    *,
    frames: int = DEFAULT_FULL_FRAMES,
    want_audio: bool = False,
    longest_edge: int = DEFAULT_LONGEST_EDGE,
    dedup_distance: int = DEFAULT_DEDUP_DISTANCE,
) -> StreamSample:
    """Sample ≤ `frames` stills spread evenly across a whole finite video — the
    analyze_video "cover the clip" shape, for the "analyze this YouTube video" case.

    Each frame is a fast discrete `-ss` seek-grab at the midpoint of its even bucket,
    stamped at its true offset; the set is then deduped. Audio (when `want_audio`) is
    the whole track as WAV, but only when the video is short enough to transcribe
    in-turn (`MAX_FULL_AUDIO_S`) — a longer video returns frames-only. Refuses a live
    stream or a video of unknown duration (use window mode there). Returns empty
    frames (never raises for a decode miss)."""
    if resolved.is_live or not resolved.duration_s:
        raise StreamError("full analysis needs a finite video — use window mode for a live stream")
    if not ffmpeg_available():
        log.info("stream.sample_skipped", reason="ffmpeg unavailable")
        return StreamSample(frames=[])

    count = max(1, min(frames, MAX_FRAMES))
    duration = resolved.duration_s
    with tempfile.TemporaryDirectory(prefix="jbrain-stream-") as tmp:
        tmpdir = Path(tmp)
        sampled: list[SampledFrame] = []
        for i in range(count):
            at = duration * (i + 0.5) / count  # midpoint of each even bucket
            jpeg = await _grab_one(
                resolved.media_url,
                tmpdir,
                at=at,
                longest_edge=longest_edge,
                headers=resolved.http_headers,
            )
            if jpeg is not None:
                sampled.append(SampledFrame(timestamp_ms=int(at * 1000), jpeg=jpeg))
        deduped = dedup_frames(sampled, distance=dedup_distance)
        audio = b""
        if want_audio and duration <= MAX_FULL_AUDIO_S:
            audio = await _extract_audio(
                resolved.media_url,
                tmpdir,
                window=duration,
                seek=0.0,
                headers=resolved.http_headers,
            )
    return StreamSample(frames=deduped, audio_wav=audio)


def _input_guard_args(media_url: str) -> list[str]:
    """ffmpeg input args restricting a URL to network protocols only (`_URL_PROTOCOLS`),
    so a crafted manifest can't reach a `file:`/`pipe:`/`concat:`/`data:` target. A
    local file path (tests pass one as the media URL) is left unrestricted — the
    whitelist would otherwise bar the `file` protocol it needs."""
    if media_url.startswith(("http://", "https://")):
        return ["-protocol_whitelist", _URL_PROTOCOLS]
    return []


def _header_args(headers: dict[str, str]) -> list[str]:
    """ffmpeg input args carrying yt-dlp's request headers — the User-Agent via
    `-user_agent` and the rest as CRLF-joined `-headers` — so ffmpeg fetches a signed
    googlevideo URL with the same identity yt-dlp resolved it under. Without this a
    windowed/audio read of an ANDROID_VR-client URL 403s. Empty (a local file / no
    headers) adds nothing."""
    if not headers:
        return []
    args: list[str] = []
    ua = next((v for k, v in headers.items() if k.lower() == "user-agent"), "")
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent")
    if ua:
        args += ["-user_agent", ua]
    if extra:
        args += ["-headers", extra]
    return args


async def _grab_one(
    media_url: str, tmpdir: Path, *, at: float, longest_edge: int, headers: dict[str, str]
) -> bytes | None:
    """One fast frame at offset `at` via input seek (`-ss` before `-i`), or None on a
    decode miss. Bounded by the same per-read and wall-clock timeouts as the window
    pass, so a stalled host can't hang the grab; the ffmpeg leg is cancel-safe."""
    out = tmpdir / f"grab_{int(at * 1000):09d}.jpg"
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-rw_timeout",
        str(_RW_TIMEOUT_US),
        *_input_guard_args(media_url),
        *_header_args(headers),
        "-ss",
        f"{at:.3f}",
        "-i",
        media_url,
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({longest_edge},iw)':-2",
        "-q:v",
        "3",
        str(out),
    ]
    try:
        rc, _, _ = await run_media_proc(cmd, timeout_s=_FFMPEG_SLACK_S)
    except (OSError, TimeoutError) as exc:
        log.info("stream.grab_failed", at=round(at, 2), error=str(exc))
        return None
    if rc != 0:
        log.info("stream.grab_failed", at=round(at, 2), rc=rc)
        return None
    try:
        return out.read_bytes()
    except OSError:
        return None


async def _extract_frames(
    media_url: str,
    tmpdir: Path,
    *,
    frames: int,
    window: float,
    seek: float,
    longest_edge: int,
    headers: dict[str, str],
) -> list[SampledFrame]:
    """Run one bounded ffmpeg pass over the media window, returning window-relative
    stamped JPEGs. A single frame skips the fps filter (grab one still); a multi-frame
    window spreads `frames` evenly across it via `fps = frames / window`."""
    pattern = tmpdir / "frame_%05d.jpg"
    scale = f"scale='min({longest_edge},iw)':-2"  # never upscale; codec-safe even height
    single = frames <= 1 or window <= 0
    fps = 1.0 if single else max(frames / window, 0.02)
    vf = scale if single else f"fps={fps:.6f},{scale}"

    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-rw_timeout", str(_RW_TIMEOUT_US)]
    cmd += _input_guard_args(media_url)
    cmd += _header_args(headers)
    if seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]  # input seek (before -i): fast, VOD only
    cmd += ["-i", media_url]
    if not single and window > 0:
        cmd += ["-t", f"{window:.3f}"]
    cmd += ["-vf", vf, "-vsync", "vfr", "-frames:v", str(frames), "-q:v", "3", str(pattern)]

    timeout = int(window + _FFMPEG_SLACK_S) if not single else _FFMPEG_SLACK_S
    try:
        rc, _, stderr = await run_media_proc(cmd, timeout_s=timeout)
    except (OSError, TimeoutError) as exc:  # timeout / spawn failure
        log.warning("stream.frames_failed", error=str(exc))
        return []
    if rc != 0:
        log.warning("stream.frames_failed", stderr=stderr.decode("utf-8", "replace")[-500:])
        return []

    paths = _sorted_jpegs(tmpdir)
    out: list[SampledFrame] = []
    for i, p in enumerate(paths):
        seconds = 0.0 if single else i / fps
        out.append(SampledFrame(timestamp_ms=int(seconds * 1000), jpeg=p.read_bytes()))
    return out


async def _extract_audio(
    media_url: str, tmpdir: Path, *, window: float, seek: float, headers: dict[str, str]
) -> bytes:
    """Best-effort: pull the window's audio as 16 kHz mono WAV (whisper's native shape)
    with a second bounded ffmpeg pass. Returns b"" when the media has no audio track or
    the pass fails — the caller then runs frames-only. A window of 0 grabs nothing."""
    if window <= 0:
        return b""
    out = tmpdir / "audio.wav"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-rw_timeout", str(_RW_TIMEOUT_US)]
    cmd += _input_guard_args(media_url)
    cmd += _header_args(headers)
    if seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += [
        "-i",
        media_url,
        "-t",
        f"{window:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-f",
        "wav",
        str(out),
    ]
    try:
        rc, _, _ = await run_media_proc(cmd, timeout_s=int(window + _FFMPEG_SLACK_S))
    except (OSError, TimeoutError) as exc:
        log.info("stream.audio_failed", error=str(exc))
        return b""
    if rc != 0:
        log.info("stream.audio_failed", rc=rc)
        return b""
    try:
        data = out.read_bytes()
    except OSError:
        return b""
    # A header-only WAV (no samples) is ~44 bytes — treat as "no audio", not a clip.
    return data if len(data) > 44 else b""
