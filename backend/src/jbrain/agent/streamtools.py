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

import structlog

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.ingest.video import (
    VideoAnalysis,
    caption_frames,
    fuse_and_reduce,
    transcribe_audio,
)
from jbrain.llm import LlmRouter
from jbrain.llm.local_gateway import LocalGateway
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
_AUDIO_MEDIA_TYPE = "audio/wav"


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

        if report:
            report(0, 0, "Opening stream…")
        try:
            resolved = await asyncio.to_thread(resolve, url, max_height=max_height)
        except StreamError as exc:
            return str(exc)

        want_audio = (
            mode != "single"
            and bool(arguments.get("transcribe", True))
            and (transcribe is not None)
        )
        try:
            sample = await _sample(
                mode, resolved, arguments, want_audio, window_sampler, full_sampler
            )
        except StreamError as exc:
            return str(exc)
        if not sample.frames and not sample.audio_wav:
            return f'I couldn\'t read any frames from that stream ("{resolved.title}").'

        captioned = await caption_frames(
            sample.frames, filename=resolved.title, router=router, blobs=blobs, on_progress=report
        )
        transcript = None
        if want_audio and sample.audio_wav:
            if report:
                report(0, 0, "Transcribing audio…")
            transcript = await transcribe_audio(
                transcribe,
                gateway,
                transcribe_model,
                sample.audio_wav,
                filename="stream-audio.wav",
                media_type=_AUDIO_MEDIA_TYPE,
            )

        result = await fuse_and_reduce(captioned, transcript, router=router, on_progress=report)
        if result is None:
            return f'I couldn\'t make anything of that stream ("{resolved.title}").'
        return ToolOutput(
            _summary_line(resolved.title, result), view=_stream_view(resolved, result, mode)
        )

    return {"analyze_stream": analyze_stream_tool}


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


def _stream_view(resolved: ResolvedStream, result: VideoAnalysis, mode: str) -> ViewPayload:
    """The `video_analysis` card, reused for a stream source: the summary, the per-frame
    timeline {t_ms, caption, thumb_id}, and the transcript — plus the stream's page URL
    and live flag for the source chip (Wave 3 renders it). Ids only, no URLs on the
    frames (the component builds thumb srcs); the page URL is the owner-facing source,
    not a render-time fetch (invariant #9)."""
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
            "summary": result.summary,
            "duration_ms": analysis.get("duration_ms"),
            "frames": analysis.get("frames", []),
            "transcript": analysis.get("transcript"),
        },
    )
