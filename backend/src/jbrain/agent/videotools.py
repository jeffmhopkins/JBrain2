"""The `analyze_video` agent tool: jerv reads an attached video by sampling its
frames and transcribing its audio (docs/archive/VIDEO_ANALYSIS_PLAN.md, Wave 3).

The video sibling of `analyze_image`/`transcribe`: it resolves a chat attachment by
id under the session's RLS scope (a foreign/out-of-scope id reads as a clean miss,
never a leak), fetches its bytes, and runs the map→fuse→reduce inline
(jbrain.ingest.video.run_video_analysis) — sample + caption frames, transcribe the
audio, fuse on a timeline, summarize. Like the other chat-media tools the work runs
in the turn (a chat attachment has no note pipeline / cache behind it); a clip can
take a little while, which the sidecar warns the model about. The model reads the
summary; the owner sees the scrubbing card built from the structured analysis (the
attachment id + per-frame thumbs + transcript — ids only, no URLs, invariant #9).

Wired only when ffmpeg can sample frames; the registry drops the sidecar otherwise
(graceful degrade, like the image/whisper tools). Whisper is optional — without it
the analysis is frames-only.
"""

import uuid
from typing import Any

import structlog

from jbrain.agent.attachments import AttachmentInfo, TurnAttachmentRepo, is_video_media_type
from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.ingest.video import ProgressFn, VideoSampler, run_video_analysis
from jbrain.llm import LlmRouter
from jbrain.llm.local_gateway import LocalGateway
from jbrain.media import sample_frames
from jbrain.storage import BlobStore
from jbrain.transcribe import TranscribeClient

log = structlog.get_logger()

_NO_VIDEO = "No attached video with that id is in this chat."
_NOT_VIDEO = "That attachment isn't a video — analyze_video only reads video files."
# The clip ceiling, the transcribe tool's sibling: refuse an oversized file up front
# rather than block the turn on a multi-minute decode the gateway would kill anyway.
DEFAULT_TOOL_MAX_BYTES = 500 * 1024 * 1024


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def build_video_handlers(
    blobs: BlobStore,
    attachments: TurnAttachmentRepo,
    router: LlmRouter,
    *,
    transcribe: TranscribeClient | None = None,
    transcribe_model: str = "",
    gateway: LocalGateway | None = None,
    sampler: VideoSampler | None = None,
    max_bytes: int = DEFAULT_TOOL_MAX_BYTES,
) -> dict[str, ToolHandler]:
    """The `analyze_video` handler, bound to its services. `transcribe`/`gateway` are
    optional (frames-only without whisper, the same best-effort posture as the audio
    tool); `sampler` defaults to the ffmpeg frame sampler; `max_bytes` caps the clip."""
    frame_sampler: VideoSampler = sampler or sample_frames

    async def analyze_video_tool(arguments: dict, ctx: ToolContext) -> str:
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        # When false, the analysis still runs (the model reads the summary) but the
        # scrubbing card is suppressed — for when the video read is an intermediate step
        # toward the answer, not something the owner needs to see (VIDEO_IMAGE_TOOLS_PLAN.md).
        show = arguments.get("show", True) is not False
        if ctx.agent_session_id is None or not _is_uuid(attachment_id):
            return _NO_VIDEO
        # A chat attachment is domain-scoped, so it is read under the session's
        # attachment context; RLS hides a foreign id as a clean miss.
        att_ctx = await attachments.session_read_context(ctx.session, ctx.agent_session_id)
        if att_ctx is None:
            return _NO_VIDEO
        info = await attachments.get(att_ctx, attachment_id)
        if info is None:
            return _NO_VIDEO
        if not is_video_media_type(info.media_type):
            return _NOT_VIDEO

        # A re-ask reads the cached analysis (persisted on the attachment) — free, and
        # it's also what lets the card's thumbnails be served (the thumbnail endpoint
        # validates a thumb_id against this stored frame list under the firewall).
        cached = await attachments.analysis(att_ctx, attachment_id)
        if cached is not None:
            return ToolOutput(
                _summary_line(info.filename, cached),
                view=_video_view(attachment_id, info, cached) if show else None,
            )

        if info.size_bytes > max_bytes:
            return "That video is too large to analyze."
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            return "That file is no longer available."
        # Stream each phase ("Extracting frames…", "Analyzing frame 12/30", …) into the
        # turn as a live status the card replaces on completion. Only on the streaming
        # path (ctx.emit_progress set); the batch path reports nothing.
        on_progress: ProgressFn | None = None
        sink = ctx.emit_progress
        if sink is not None:
            on_progress = lambda step, total, label: sink(step, total, None, label)  # noqa: E731
        try:
            result = await run_video_analysis(
                data,
                filename=info.filename,
                media_type=info.media_type,
                router=router,
                blobs=blobs,
                sampler=frame_sampler,
                transcribe=transcribe,
                transcribe_model=transcribe_model,
                gateway=gateway,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001 - a tool error is a recoverable observation
            log.warning("analyze_video_tool_failed", error=repr(exc))
            return "I couldn't analyze that video right now — the local models didn't respond."
        if result is None:
            return f'I couldn\'t read any frames or speech from "{info.filename}".'
        stored = {"summary": result.summary, **result.analysis}
        await attachments.set_analysis(att_ctx, attachment_id, stored)
        # The model reads the summary; the owner sees the rich scrubbing card. The view
        # carries the attachment id + structured analysis, never a URL — the component
        # builds the media/thumbnail srcs (invariant #9).
        return ToolOutput(
            _summary_line(info.filename, stored),
            view=_video_view(attachment_id, info, stored) if show else None,
        )

    return {"analyze_video": analyze_video_tool}


def _summary_line(filename: str, analysis: dict[str, Any]) -> str:
    summary = str(analysis.get("summary") or "").strip()
    if not summary:
        return f'"{filename}" has no speech or clearly described content.'
    return f'Analysis of "{filename}":\n{summary}'


def _video_view(attachment_id: str, info: AttachmentInfo, analysis: dict[str, Any]) -> ViewPayload:
    """The `video_analysis` card payload (docs/mocks/analyze-video-approved.html): the
    summary, the per-frame timeline {t_ms, caption, thumb_id}, and the transcript for
    the karaoke tab. Ids only, no URLs — the component builds the media/thumb srcs."""
    return ViewPayload(
        view="video_analysis",
        surface="inline",
        data={
            "attachment_id": attachment_id,
            "source": "chat",
            "media": "video",
            "filename": info.filename,
            "summary": analysis.get("summary", ""),
            "duration_ms": analysis.get("duration_ms"),
            "frames": analysis.get("frames", []),
            "transcript": analysis.get("transcript"),
        },
    )
