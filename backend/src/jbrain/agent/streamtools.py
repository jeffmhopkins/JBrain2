"""The `analyze_stream` agent tool: jerv reads a video **URL** — a live stream or an
on-demand video — by sampling frames (and optionally its audio) and running the same
caption→fuse→reduce as `analyze_video` (docs/archive/STREAM_ANALYSIS_PLAN.md, Wave 2).

The URL sibling of `analyze_video` (which reads an attachment). Where the attachment
tool resolves a chat-scoped blob, this resolves a model-supplied URL with yt-dlp and
samples it with ffmpeg (`jbrain.stream`) — the second sanctioned direct outbound leg
of the jerv sandbox, so the resolved media host passes the shared SSRF guard before
ffmpeg opens it (invariant #9). Three modes:

  single  one frame (live edge / a VOD seek point) — the fast "what does it show now?"
  window  N frames across a Y-second window (+ optional whisper on that window's audio)
  full    frames spread across a whole finite VOD (+ optional whisper, short VOD only)

The heavy work (resolve, ffmpeg sampling) is blocking, so it runs off the event loop.
Like the other chat-media tools there is no note pipeline / cache behind a URL, so the
analysis runs in the turn; a stream can take a little while, which the sidecar warns
the model about. The model reads the summary; the owner sees the analysis card (Wave 3
completes the stream source chip). Frames carry `thumb_id` blob ids only, never URLs.
"""

import asyncio
import base64

import structlog

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.ingest.video import (
    ProgressFn,
    VideoAnalysis,
    caption_frames,
    fuse_and_reduce,
    transcribe_audio_chunked,
)
from jbrain.llm import LlmRouter
from jbrain.llm.local_gateway import LocalGateway
from jbrain.media import SampledFrame, jpeg_thumbnail
from jbrain.storage import BlobStore
from jbrain.stream import (
    DEFAULT_FULL_FRAMES,
    DEFAULT_MAX_HEIGHT,
    DEFAULT_WINDOW_FRAMES,
    DEFAULT_WINDOW_S,
    MAX_FRAMES,
    MAX_WINDOW_S,
    ResolvedStream,
    Resolver,
    StreamError,
    StreamSample,
    sample_stream,
    sample_stream_full,
)
from jbrain.transcribe import TranscribeClient

log = structlog.get_logger()

_MODES = ("single", "window", "full")


def build_stream_handlers(
    blobs: BlobStore,
    router: LlmRouter,
    *,
    transcribe: TranscribeClient | None = None,
    transcribe_model: str = "",
    gateway: LocalGateway | None = None,
    resolver: Resolver | None = None,
    window_sampler=sample_stream,
    full_sampler=sample_stream_full,
    max_height: int = DEFAULT_MAX_HEIGHT,
) -> dict[str, ToolHandler]:
    """The `analyze_stream` handler bound to its services. `transcribe`/`gateway` are
    optional (frames-only without whisper). `resolver`/`window_sampler`/`full_sampler`
    are injectable so tests drive the handler without yt-dlp or ffmpeg."""
    from jbrain.stream import resolve_stream

    resolve = resolver or resolve_stream

    async def analyze_stream_tool(arguments: dict, ctx: ToolContext) -> str:
        url = str(arguments.get("url", "")).strip()
        if not url:
            return "analyze_stream needs a url."
        mode = str(arguments.get("mode", "window")).strip().lower() or "window"
        if mode not in _MODES:
            return "mode must be one of: single, window, full."

        sink = ctx.emit_progress
        report = (
            (lambda step, total, label: sink(step, total, None, label))
            if sink is not None
            else None
        )

        want_audio = (
            mode != "single"
            and bool(arguments.get("transcribe", True))
            and (transcribe is not None)
        )
        if report:
            report(0, 0, "Opening stream…")
        # One guard over the whole pipeline: a StreamError carries an actionable message
        # (bad URL, live-in-full, a non-public resolved host); anything else — a network
        # blip resolving a host, a local model that didn't answer — is turned into a
        # clean recoverable observation instead of leaking a raw exception (e.g. a bare
        # "[Errno -3] Temporary failure in name resolution") to the model, matching
        # analyze_video's posture.
        try:
            resolved = await asyncio.to_thread(resolve, url, max_height=max_height)
            sample = await _sample(
                mode, resolved, arguments, want_audio, window_sampler, full_sampler
            )
            if not sample.frames and not sample.audio_wav:
                return f'I couldn\'t read any frames from that stream ("{resolved.title}").'
            captioned = await caption_frames(
                sample.frames,
                filename=resolved.title,
                router=router,
                blobs=blobs,
                on_progress=report,
            )
            # Whisper is best-effort (the "frames-only without whisper" contract): a
            # transcription failure degrades to frames-only, it never kills an otherwise
            # good frame analysis.
            transcript = None
            if want_audio and sample.audio_wav:
                if report:
                    report(0, 0, "Transcribing audio…")
                transcript = await _transcribe_best_effort(
                    transcribe, gateway, transcribe_model, sample.audio_wav, on_progress=report
                )
            result = await fuse_and_reduce(captioned, transcript, router=router, on_progress=report)
        except StreamError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - a tool error is a recoverable observation
            log.warning("analyze_stream_failed", error=repr(exc))
            return (
                "I couldn't analyze that stream right now — the video host or the local "
                "models couldn't be reached."
            )
        if result is None:
            return f'I couldn\'t make anything of that stream ("{resolved.title}").'
        return ToolOutput(
            _summary_line(resolved.title, result),
            view=_stream_view(resolved, result, mode, sample.frames),
        )

    return {"analyze_stream": analyze_stream_tool}


async def _transcribe_best_effort(
    transcribe: TranscribeClient | None,
    gateway: LocalGateway | None,
    transcribe_model: str,
    audio_wav: bytes,
    *,
    on_progress: ProgressFn | None = None,
) -> dict | None:
    """Transcribe the audio segment, degrading to None (frames-only) on ANY failure —
    a whisper/network hiccup must not sink an otherwise-good frame analysis. Long audio
    (a full-video pass) is transcribed in chunks so no single whisper call runs past its
    timeout, with per-chunk progress; short audio takes the plain single call."""
    try:
        return await transcribe_audio_chunked(
            transcribe,
            gateway,
            transcribe_model,
            audio_wav,
            filename="stream-audio.wav",
            on_progress=on_progress,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort: fall back to frames-only
        log.info("stream.transcribe_failed", error=repr(exc))
        return None


async def _sample(
    mode: str,
    resolved: ResolvedStream,
    arguments: dict,
    want_audio: bool,
    window_sampler,
    full_sampler,
) -> StreamSample:
    """Dispatch the requested mode to the right (blocking) sampler, off the event loop.
    `single` and `window` share the windowed sampler (single = one frame, no window);
    `full` covers a whole finite VOD."""
    if mode == "full":
        frames = _clamp_frames(arguments.get("frames"), DEFAULT_FULL_FRAMES)
        return await asyncio.to_thread(
            lambda: full_sampler(resolved, frames=frames, want_audio=want_audio)
        )
    if mode == "single":
        return await asyncio.to_thread(
            lambda: window_sampler(resolved, frames=1, window_s=0.0, want_audio=False)
        )
    frames = _clamp_frames(arguments.get("frames"), DEFAULT_WINDOW_FRAMES)
    window = _clamp_window(arguments.get("window_s"))
    seek = _positive_float(arguments.get("seek"))
    return await asyncio.to_thread(
        lambda: window_sampler(
            resolved, frames=frames, window_s=window, seek_s=seek, want_audio=want_audio
        )
    )


def _clamp_frames(value: object, default: int) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_FRAMES))


def _clamp_window(value: object) -> float:
    try:
        w = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        w = DEFAULT_WINDOW_S
    return max(1.0, min(w, MAX_WINDOW_S))


def _positive_float(value: object) -> float:
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _summary_line(title: str, result: VideoAnalysis) -> str:
    summary = (result.summary or "").strip()
    if not summary:
        return f'"{title}" had no clearly describable content.'
    return f'Analysis of "{title}":\n{summary}'


def _stream_view(
    resolved: ResolvedStream, result: VideoAnalysis, mode: str, sampled: list[SampledFrame]
) -> ViewPayload:
    """The `video_analysis` card, reused for a stream source: the summary, the per-frame
    timeline {t_ms, caption, thumb_data_uri}, and the transcript — plus the stream's page
    URL and live flag. A stream has no attachment and so no served-thumbnail route, so
    each frame carries a **small inline thumbnail data URI** (server-built, downscaled) —
    the card shows the actual still while triggering no render-time external fetch
    (invariant #9), the same pattern the image-gen preview uses. The page URL is the
    owner-facing source, not a fetched resource.

    For a **YouTube** source the card also embeds the provider's player (`youtube_id`),
    synced to the timeline via postMessage — a bounded, owner-approved exception to #9
    (docs/reference/ASSISTANT.md): the id is server-derived from the yt-dlp resolve, not
    model-authored, and the iframe is browser origin-isolated. Empty for non-YouTube."""
    analysis = result.analysis
    return ViewPayload(
        view="video_analysis",
        surface="inline",
        data={
            "source": "stream",
            "media": "video",
            "filename": resolved.title,
            "stream_url": resolved.webpage_url,
            "is_live": resolved.is_live,
            "mode": mode,
            "youtube_id": _youtube_id(resolved),
            "summary": result.summary,
            "duration_ms": analysis.get("duration_ms"),
            "frames": _frames_with_thumbs(analysis.get("frames", []), sampled),
            "transcript": analysis.get("transcript"),
        },
    )


def _youtube_id(resolved: ResolvedStream) -> str:
    """The YouTube video id to embed, or "" for a non-YouTube source. yt-dlp's youtube
    extractor reports `provider == "youtube"` and an 11-char `video_id`; a live stream
    is embeddable too. Anything else keeps the still card with no player."""
    if resolved.provider == "youtube" and resolved.video_id:
        return resolved.video_id
    return ""


def _frames_with_thumbs(captioned: list[dict], sampled: list[SampledFrame]) -> list[dict]:
    """Attach a compact inline thumbnail (`thumb_data_uri`) to each captioned frame from
    its sampled JPEG bytes — `captioned` is 1:1 and in order with `sampled` (the handler
    captions exactly the deduped frames it sampled). The still is downscaled so the data
    URI stays a few KB; `thumb_id` is kept for parity with the attachment payload."""
    out: list[dict] = []
    for cap, frame in zip(captioned, sampled, strict=False):
        thumb = jpeg_thumbnail(frame.jpeg)
        out.append(
            {
                **cap,
                "thumb_data_uri": "data:image/jpeg;base64," + base64.b64encode(thumb).decode(),
            }
        )
    return out
