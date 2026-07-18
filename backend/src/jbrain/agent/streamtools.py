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
  full    frames spread across a whole finite VOD (+ optional whisper)

**Short grabs run in the turn; long passes defer** (DEFERRED_TOOL_CALLS_PLAN.md P2). A
`single` grab or a short `window` is fast, so it runs inline and returns its
`video_analysis` card immediately — latency is the point. A `full` whole-video pass or a
long `window` is minutes of ffmpeg + whisper, so instead of blocking the turn it kicks a
background job (the shared `stream_analysis` pipeline on the worker), **ends the turn**
with a live `task_status` card, and the finished analysis auto-resumes into the chat. The
model doesn't choose — the tool routes by mode/window. Both paths build the identical
card, so a deferred full analysis is indistinguishable from an in-turn one once done.
Frames carry inline downscaled thumbnails only, never URLs.
"""

import asyncio

import structlog

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import DeferredRef, ToolContext, ToolHandler, ToolOutput
from jbrain.agent.media_results import MediaResults
from jbrain.ingest.stream_analysis import (
    KIND_ANALYZE_STREAM_URL,
    MODES,
    build_stream_view_data,
    clamp_window,
    run_stream_pipeline,
    summary_line,
)
from jbrain.llm import LlmRouter
from jbrain.llm.local_gateway import LocalGateway
from jbrain.queue import JobEnqueuer
from jbrain.storage import BlobStore
from jbrain.stream import (
    DEFAULT_MAX_HEIGHT,
    Resolver,
    StreamError,
    resolve_stream,
    sample_stream,
    sample_stream_full,
)
from jbrain.transcribe import TranscribeClient

log = structlog.get_logger()

# The in-turn ↔ defer boundary. `single` and a short `window` stay in-turn (latency is the
# point); `full` and any `window` longer than this defer to the background job. The model
# never sees this knob — the tool routes by estimated cost, per the plan's threshold.
_DEFER_WINDOW_S = 30.0


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
    queue: JobEnqueuer | None = None,
    media_results: MediaResults | None = None,
) -> dict[str, ToolHandler]:
    """The `analyze_stream` handler bound to its services. `transcribe`/`gateway` are
    optional (frames-only without whisper). `queue`/`media_results` enable the deferred
    path for long analyses — without them (or an anonymous session) every mode runs
    in-turn. `resolver`/`window_sampler`/`full_sampler` are injectable so tests drive the
    handler without yt-dlp or ffmpeg."""
    resolve = resolver or resolve_stream
    can_defer = queue is not None and media_results is not None

    async def analyze_stream_tool(arguments: dict, ctx: ToolContext) -> str:
        url = str(arguments.get("url", "")).strip()
        if not url:
            return "analyze_stream needs a url."
        mode = str(arguments.get("mode", "window")).strip().lower() or "window"
        if mode not in MODES:
            return "mode must be one of: single, window, full."

        # Route the expensive whole-video / long-window passes off-turn: kick a background
        # job, end the turn, and let the task_status card + auto-resume take over. Falls
        # back to in-turn when the queue/results store or a chat session isn't available.
        if _should_defer(mode, arguments) and can_defer and ctx.agent_session_id:
            assert queue is not None and media_results is not None
            return await _kick_deferred(url, mode, arguments, ctx, queue, media_results)

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
        # blip, a local model that didn't answer — is turned into a clean recoverable
        # observation rather than leaking a raw exception to the model.
        try:
            resolved = await asyncio.to_thread(resolve, url, max_height=max_height)
            out = await run_stream_pipeline(
                resolved,
                mode,
                arguments,
                want_audio=want_audio,
                router=router,
                blobs=blobs,
                transcribe=transcribe,
                gateway=gateway,
                transcribe_model=transcribe_model,
                window_sampler=window_sampler,
                full_sampler=full_sampler,
                on_progress=report,
            )
        except StreamError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - a tool error is a recoverable observation
            log.warning("analyze_stream_failed", error=repr(exc))
            return (
                "I couldn't analyze that stream right now — the video host or the local "
                "models couldn't be reached."
            )
        if out is None:
            return f'I couldn\'t read any frames from that stream ("{resolved.title}").'
        result, frames = out
        return ToolOutput(
            summary_line(resolved.title, result),
            view=ViewPayload(
                view="video_analysis",
                surface="inline",
                data=build_stream_view_data(resolved, result, mode, frames),
            ),
        )

    return {"analyze_stream": analyze_stream_tool}


def _should_defer(mode: str, arguments: dict) -> bool:
    """Whether this request is expensive enough to run off-turn. `full` always defers (a
    whole-video caption + transcribe is minutes); a `window` defers once it's longer than
    the in-turn budget. `single` and a short `window` never defer."""
    if mode == "full":
        return True
    if mode == "window":
        return clamp_window(arguments.get("window_s")) > _DEFER_WINDOW_S
    return False


async def _kick_deferred(
    url: str,
    mode: str,
    arguments: dict,
    ctx: ToolContext,
    queue: JobEnqueuer,
    media_results: MediaResults,
) -> ToolOutput:
    """Open a result row, enqueue the analyze_stream_url job, and return a `deferred`
    result: the loop streams the task_status card and ends the turn, the worker runs the
    analysis, and the finished card auto-resumes into this chat. The row is created and
    the job enqueued under the turn's owner session (RLS: owner-only, like app.jobs)."""
    session_id = str(ctx.agent_session_id)
    result_id = await media_results.create(ctx.session, session_id=session_id)
    payload: dict = {"result_id": result_id, "url": url, "mode": mode}
    for key in ("frames", "window_s", "seek"):
        if key in arguments:
            payload[key] = arguments[key]
    job_id = await queue.enqueue(ctx.session, KIND_ANALYZE_STREAM_URL, payload)
    await media_results.attach_job(ctx.session, result_id, job_id)

    view = ViewPayload(
        view="task_status",
        surface="inline",
        data={
            "result_id": result_id,
            "result_view": "video_analysis",  # the component the card swaps to when done
            "title": "Analyzing video",
            "state": "running",
            "url": url,
            "mode": mode,
        },
    )
    return ToolOutput(
        "Analyzing that video now — I'll share the summary and transcript here as soon as"
        " it's ready.",
        view=view,
        deferred=DeferredRef(job_id=job_id, result_id=result_id, session_id=session_id),
    )
