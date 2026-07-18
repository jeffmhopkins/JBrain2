"""The shared URL video-analysis pipeline for `analyze_stream`, run **two ways**:

  in-turn  a short grab (`single`, short `window`) runs inside the chat turn — latency
           is the point — and returns its `video_analysis` card immediately.
  deferred a whole-video (`full`) or long `window` pass is too slow for a turn, so it
           defers to a background job (DEFERRED_TOOL_CALLS_PLAN.md P2): the turn ends,
           a `task_status` card shows progress, and the finished analysis auto-resumes.

Both paths run the **same** resolve→sample→caption→transcribe→reduce and build the
**same** card data, so a full video analyzed in-turn and one analyzed off-turn are
indistinguishable in the chat. This module is the sibling of `ingest.video`'s
`VideoPipeline` (the attachment path) for the URL path — the agent tool
(`agent.streamtools`) drives the in-turn path, the worker drives the deferred one.

Cancellation is real without any worker surgery: the worker awaits the job handler
like any other, and the handler itself races the analysis against a **cancel watcher**
that polls the result row and `.cancel()`s the analysis task when the owner taps Stop.
That cancellation propagates into the awaited ffmpeg/whisper legs, which P1 made
cancel-safe — so a Stop terminates the subprocess promptly instead of orphaning it.
"""

from __future__ import annotations

import asyncio
import base64

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent import media_results
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
from jbrain.queue import SYSTEM_CTX
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
    resolve_stream,
    sample_stream,
    sample_stream_full,
)
from jbrain.transcribe import TranscribeClient
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

MODES = ("single", "window", "full")

KIND_ANALYZE_STREAM_URL = "analyze_stream_url"

# In-code only (NOT an app.actions seed — 0035's seed-lockstep is untouched), the URL
# sibling of analyze_video_attachment: kicked on demand by a deferred analyze_stream,
# never by a seeded pipeline. The worker adds it to its build_registry tuple.
ANALYZE_STREAM_URL_SPEC = ActionSpec(
    name=KIND_ANALYZE_STREAM_URL,
    version=1,
    handler=KIND_ANALYZE_STREAM_URL,
    domain_optional=True,
    mutating=True,
    cost_class="expensive",
    dedup_key_expr="result_id",
    description="Analyze a whole video URL off-turn: caption sampled frames, transcribe"
    " the audio, summarize the fused timeline, and store the result for its status card.",
)

# How often the worker's cancel watcher polls the result row for a Stop. Frequent enough
# that a tap lands within a couple of seconds; cheap enough (one owner-scoped SELECT).
_CANCEL_POLL_S = 2.0


# --- argument clamps + mode dispatch (shared by the in-turn and deferred paths) -------


def clamp_frames(value: object, default: int) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_FRAMES))


def clamp_window(value: object) -> float:
    try:
        w = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        w = DEFAULT_WINDOW_S
    return max(1.0, min(w, MAX_WINDOW_S))


def positive_float(value: object) -> float:
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


async def sample_for_mode(
    mode: str,
    resolved: ResolvedStream,
    arguments: dict,
    want_audio: bool,
    window_sampler=sample_stream,
    full_sampler=sample_stream_full,
) -> StreamSample:
    """Dispatch the requested mode to the right sampler (async, so its ffmpeg legs are
    cancel-safe). `single`/`window` share the windowed sampler; `full` covers a whole
    finite VOD."""
    if mode == "full":
        frames = clamp_frames(arguments.get("frames"), DEFAULT_FULL_FRAMES)
        return await full_sampler(resolved, frames=frames, want_audio=want_audio)
    if mode == "single":
        return await window_sampler(resolved, frames=1, window_s=0.0, want_audio=False)
    frames = clamp_frames(arguments.get("frames"), DEFAULT_WINDOW_FRAMES)
    window = clamp_window(arguments.get("window_s"))
    seek = positive_float(arguments.get("seek"))
    return await window_sampler(
        resolved, frames=frames, window_s=window, seek_s=seek, want_audio=want_audio
    )


# --- the pipeline core + the card data both paths produce -----------------------------


async def transcribe_best_effort(
    transcribe: TranscribeClient | None,
    gateway: LocalGateway | None,
    transcribe_model: str,
    audio_wav: bytes,
    *,
    on_progress: ProgressFn | None = None,
) -> dict | None:
    """Transcribe the audio, degrading to None (frames-only) on ANY failure — a
    whisper/network hiccup must not sink an otherwise-good frame analysis. Long audio (a
    full-video pass) is chunked so no single whisper call runs past its timeout."""
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


async def run_stream_pipeline(
    resolved: ResolvedStream,
    mode: str,
    arguments: dict,
    *,
    want_audio: bool,
    router: LlmRouter,
    blobs: BlobStore,
    transcribe: TranscribeClient | None,
    gateway: LocalGateway | None,
    transcribe_model: str,
    window_sampler=sample_stream,
    full_sampler=sample_stream_full,
    on_progress: ProgressFn | None = None,
) -> tuple[VideoAnalysis, list[SampledFrame]] | None:
    """Sample→caption→transcribe→reduce one already-resolved stream. Returns the analysis
    plus the sampled frames (for the card's inline thumbnails), or None when the media
    yielded neither a frame nor any speech (nothing to summarize)."""
    sample = await sample_for_mode(
        mode, resolved, arguments, want_audio, window_sampler, full_sampler
    )
    if not sample.frames and not sample.audio_wav:
        return None
    captioned = await caption_frames(
        sample.frames, filename=resolved.title, router=router, blobs=blobs, on_progress=on_progress
    )
    transcript = None
    if want_audio and sample.audio_wav:
        if on_progress is not None:
            on_progress(0, 0, "Transcribing audio…")
        transcript = await transcribe_best_effort(
            transcribe, gateway, transcribe_model, sample.audio_wav, on_progress=on_progress
        )
    result = await fuse_and_reduce(captioned, transcript, router=router, on_progress=on_progress)
    if result is None:
        return None
    return result, sample.frames


def youtube_id(resolved: ResolvedStream) -> str:
    """The YouTube video id to embed, or "" for a non-YouTube source (still-only card)."""
    if resolved.provider == "youtube" and resolved.video_id:
        return resolved.video_id
    return ""


def _frames_with_thumbs(captioned: list[dict], sampled: list[SampledFrame]) -> list[dict]:
    """Attach a compact inline thumbnail (`thumb_data_uri`) to each captioned frame from
    its sampled JPEG bytes — `captioned` is 1:1 and in order with `sampled`. The still is
    downscaled so the data URI stays a few KB; no URL is emitted (invariant #9)."""
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


def build_stream_view_data(
    resolved: ResolvedStream, result: VideoAnalysis, mode: str, sampled: list[SampledFrame]
) -> dict:
    """The `video_analysis` card's `data` for a stream source — the summary, the per-frame
    timeline with inline thumbnails, the transcript, the stream's page URL + live flag,
    and (for YouTube) the embeddable id. Shared verbatim by the in-turn path (wrapped in a
    ViewPayload) and the deferred path (stored on the result row so the status card swaps
    to the exact same component on completion)."""
    analysis = result.analysis
    return {
        "source": "stream",
        "media": "video",
        "filename": resolved.title,
        "stream_url": resolved.webpage_url,
        "is_live": resolved.is_live,
        "mode": mode,
        "youtube_id": youtube_id(resolved),
        "summary": result.summary,
        "duration_ms": analysis.get("duration_ms"),
        "frames": _frames_with_thumbs(analysis.get("frames", []), sampled),
        "transcript": analysis.get("transcript"),
    }


def summary_line(title: str, result: VideoAnalysis) -> str:
    summary = (result.summary or "").strip()
    if not summary:
        return f'"{title}" had no clearly describable content.'
    return f'Analysis of "{title}":\n{summary}'


# --- the deferred worker job ----------------------------------------------------------


class StreamAnalysisPipeline:
    """The `analyze_stream_url` worker job (deferred path). Given a result row (opened by
    the kicking chat turn) and the resolve args, it runs the shared pipeline, streams
    progress onto the row for the `task_status` card, and writes the finished card data —
    or marks the row failed. A Stop (the row flipped to 'canceled' by the API) is honored
    promptly by an internal watcher that cancels the analysis, terminating its ffmpeg /
    whisper legs (P1). Deps mirror VideoPipeline; the samplers/resolver are injectable so
    tests drive it without ffmpeg or yt-dlp."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
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
    ) -> None:
        self._maker = maker
        self._blobs = blobs
        self._router = router
        self._transcribe = transcribe
        self._transcribe_model = transcribe_model
        self._gateway = gateway
        self._resolve = resolver or resolve_stream
        self._window_sampler = window_sampler
        self._full_sampler = full_sampler
        self._max_height = max_height

    async def analyze_stream_url(self, payload: dict) -> None:
        """Handle an analyze_stream_url job: {result_id, url, mode, frames?, window_s?,
        seek?}. A gone/cancelled result row no-ops. Best-effort throughout: any failure
        marks the row failed (never re-raises to the worker retry loop — a re-run would
        re-bill the models), and a Stop leaves the row 'canceled'."""
        result_id = str(payload["result_id"])
        row = await media_results.get(self._maker, SYSTEM_CTX, result_id)
        if row is None or row.status != "running":
            return  # reaped or already cancelled before we started

        analysis: asyncio.Task[None] = asyncio.ensure_future(self._run(payload, result_id))
        watch = asyncio.ensure_future(self._watch_cancel(result_id, analysis))
        try:
            await analysis
        except asyncio.CancelledError:
            # The watcher cancelled us on a Stop: the row is already 'canceled'. Swallow
            # (do NOT re-raise to the worker, which would fail+retry the job) and leave
            # the row as-is — the ffmpeg/whisper legs were terminated by the cancel (P1).
            log.info("stream.analysis_cancelled", result_id=result_id)
        except Exception as exc:  # noqa: BLE001 - a failed analysis marks the row, never crashes
            log.warning("stream.analysis_failed", result_id=result_id, error=repr(exc))
            await media_results.fail(self._maker, SYSTEM_CTX, result_id, error=_clean_error(exc))
        finally:
            watch.cancel()

    async def _run(self, payload: dict, result_id: str) -> None:
        url = str(payload["url"])
        mode = str(payload.get("mode", "full"))
        want_audio = mode != "single" and self._transcribe is not None

        # A queue + one drainer task turns the sync ProgressFn into ordered async writes
        # onto the result row (the emit_progress pattern) — no fire-and-forget task that
        # could be GC'd before it runs, and ticks land in order.
        progress_q: asyncio.Queue[tuple[int, int, str]] = asyncio.Queue()
        drain = asyncio.ensure_future(self._drain_progress(result_id, progress_q))
        try:
            progress_q.put_nowait((0, 0, "Opening stream…"))
            resolved = await asyncio.to_thread(self._resolve, url, max_height=self._max_height)
            out = await run_stream_pipeline(
                resolved,
                mode,
                payload,
                want_audio=want_audio,
                router=self._router,
                blobs=self._blobs,
                transcribe=self._transcribe,
                gateway=self._gateway,
                transcribe_model=self._transcribe_model,
                window_sampler=self._window_sampler,
                full_sampler=self._full_sampler,
                on_progress=lambda step, total, label: progress_q.put_nowait((step, total, label)),
            )
        finally:
            drain.cancel()

        if out is None:
            await media_results.fail(
                self._maker,
                SYSTEM_CTX,
                result_id,
                error=f'I couldn\'t read anything from that stream ("{resolved.title}").',
            )
            return
        result, frames = out
        data = build_stream_view_data(resolved, result, mode, frames)
        data["summary_line"] = summary_line(resolved.title, result)
        await media_results.complete(self._maker, SYSTEM_CTX, result_id, result=data)

    async def _drain_progress(
        self, result_id: str, queue: asyncio.Queue[tuple[int, int, str]]
    ) -> None:
        """Serialize the pipeline's progress ticks onto the result row for the card. A
        write failure is swallowed (progress is an annotation, never the job's gate)."""
        while True:
            step, total, label = await queue.get()
            try:
                await media_results.set_progress(
                    self._maker, SYSTEM_CTX, result_id, step=step, total=total, label=label
                )
            except Exception:  # noqa: BLE001 - a dropped tick must never disturb the analysis
                log.warning("stream.progress_write_failed", result_id=result_id)

    async def _watch_cancel(self, result_id: str, analysis: asyncio.Future[None]) -> None:
        """Poll the result row; when the owner taps Stop (status → 'canceled'), cancel the
        analysis so its ffmpeg/whisper legs terminate (P1). Ends when the analysis does."""
        while not analysis.done():
            await asyncio.sleep(_CANCEL_POLL_S)
            row = await media_results.get(self._maker, SYSTEM_CTX, result_id)
            if row is None or row.status == "canceled":
                analysis.cancel()
                return


def _clean_error(exc: Exception) -> str:
    """A short owner-facing failure reason — a StreamError's actionable message, else a
    generic line (never a raw errno / stack)."""
    if isinstance(exc, StreamError):
        return str(exc)
    return (
        "I couldn't analyze that stream — the video host or the local models couldn't be reached."
    )
