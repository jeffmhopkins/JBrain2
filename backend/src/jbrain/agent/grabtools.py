"""The `grab_frame` agent tool: jerv extracts a still from a video — a URL or an
attachment — at a specific timestamp and persists it as a first-class chat image
(docs/plans/VIDEO_IMAGE_TOOLS_PLAN.md, Wave V2).

The still-image sibling of `analyze_stream` (URL) and `analyze_video` (attachment):
where those return a text caption, this returns a reusable `image_id` the model can
hand to `analyze_image`/`compare_images`. It is the missing "give me the actual frame
at time T" — the gap that made jerv fabricate a comparison it couldn't perform.

Two sources, exactly one per call:
  url                  a yt-dlp-resolvable video (SSRF-guarded, like analyze_stream);
                       a live stream is refused (no fixed timestamp on a live edge).
  source_attachment_id a video the owner attached this chat (RLS-scoped; a foreign id
                       reads as a clean miss), sampled from its bytes via a temp file.

Both reuse the robust single grab from `jbrain.stream` (Wave V0: it honors `seek` and
avoids a black pre-keyframe frame). The still is stored through the shared chat-image
path (`agent.chat_images`) as a `provenance='ffmpeg'` row. An optional `question`
runs the vision read inline (grab-and-look in one hop); `show=false` suppresses the
owner-facing card when the grab is only an intermediate step.

Wired only when ffmpeg can sample frames; the registry drops the sidecar otherwise
(graceful degrade, like analyze_video/analyze_stream). yt-dlp is needed only for the
URL path — without it a URL grab fails cleanly while the attachment path still works.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.attachments import TurnAttachmentRepo, is_video_media_type
from jbrain.agent.chat_images import (
    PROVENANCE_FRAME,
    ImageTooLarge,
    UndecodableImage,
    chat_image_view,
    persist_chat_image,
)
from jbrain.agent.imagegentools import _VISION_SYSTEM, _sniff_media_type
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.llm import LlmImage, LlmRouter
from jbrain.llm.errors import LlmError
from jbrain.models.images import GeneratedImageRepo
from jbrain.storage import BlobStore
from jbrain.stream import (
    DEFAULT_MAX_HEIGHT,
    ResolvedStream,
    Resolver,
    StreamError,
    resolve_stream,
    sample_stream,
)

log = structlog.get_logger()

_NO_VIDEO = "No attached video with that id is in this chat."
_NOT_VIDEO = "That attachment isn't a video — grab_frame reads a frame from a video."
_ONE_SOURCE = (
    "grab_frame needs exactly one source: url (a video link) or source_attachment_id"
    " (a video the owner attached) — not both, not neither."
)
_LIVE = (
    "That's a live stream — there's no fixed timestamp to grab. Use analyze_stream to"
    " see what's on it now."
)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _positive_float(value: object) -> float:
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def build_grab_frame_handlers(
    blobs: BlobStore,
    attachments: TurnAttachmentRepo,
    repo: GeneratedImageRepo,
    maker: async_sessionmaker[AsyncSession],
    router: LlmRouter,
    *,
    resolver: Resolver | None = None,
    sampler=sample_stream,
    max_height: int = DEFAULT_MAX_HEIGHT,
) -> dict[str, ToolHandler]:
    """The `grab_frame` handler bound to its services. `resolver`/`sampler` are injectable
    so tests drive the handler without yt-dlp or ffmpeg; `router` runs the optional inline
    vision read (`agent.vision`)."""
    resolve = resolver or resolve_stream

    async def _grab_from_url(url: str, seek: float) -> bytes | str:
        try:
            resolved = await asyncio.to_thread(resolve, url, max_height=max_height)
        except StreamError as exc:
            return str(exc)
        if resolved.is_live:
            return _LIVE
        return await _sample_one(resolved, seek)

    async def _grab_from_attachment(
        attachment_id: str, seek: float, ctx: ToolContext
    ) -> bytes | str:
        if ctx.agent_session_id is None or not _is_uuid(attachment_id):
            return _NO_VIDEO
        att_ctx = await attachments.session_read_context(ctx.session, ctx.agent_session_id)
        if att_ctx is None:
            return _NO_VIDEO
        info = await attachments.get(att_ctx, attachment_id)
        if info is None:
            return _NO_VIDEO
        if not is_video_media_type(info.media_type):
            return _NOT_VIDEO
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            return "That file is no longer available."
        # The attachment side has no seek-to-T primitive (jbrain.media samples across the
        # whole clip), so reuse the robust stream grab pointed at the bytes on disk — a
        # local media_url ffmpeg reads the same way, and the V0 hybrid seek applies.
        with tempfile.TemporaryDirectory(prefix="jbrain-grab-") as tmp:
            path = Path(tmp) / (info.filename or "video")
            path.write_bytes(data)
            resolved = ResolvedStream(
                media_url=str(path),
                title=info.filename or "video",
                is_live=False,
                duration_s=None,
                webpage_url="",
            )
            return await _sample_one(resolved, seek)

    async def _sample_one(resolved: ResolvedStream, seek: float) -> bytes | str:
        try:
            sample = await sampler(resolved, frames=1, window_s=0.0, seek_s=seek)
        except Exception as exc:  # noqa: BLE001 - a tool error is a recoverable observation
            log.warning("grab_frame_sample_failed", error=repr(exc))
            return "I couldn't grab a frame from that video right now."
        if not sample.frames:
            at = f" at {int(seek)}s" if seek else ""
            return f"I couldn't read a frame from that video{at}."
        return sample.frames[0].jpeg

    async def _vision_read(frame: bytes, question: str) -> str:
        image = LlmImage(
            media_type=_sniff_media_type(frame), data=base64.b64encode(frame).decode()
        )
        try:
            result = await router.complete(
                "agent.vision", system=_VISION_SYSTEM, user_text=question, images=[image]
            )
        except LlmError as exc:
            # Degrade to just the image_id — the grab succeeded even if the read didn't.
            log.warning("grab_frame_vision_failed", error=str(exc))
            return ""
        return result.text.strip()

    async def grab_frame_tool(arguments: dict, ctx: ToolContext) -> str:
        url = str(arguments.get("url", "")).strip()
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        if bool(url) == bool(attachment_id):
            return _ONE_SOURCE
        seek = _positive_float(arguments.get("seek"))
        show = arguments.get("show", True) is not False
        question = str(arguments.get("question", "")).strip()

        frame = (
            await _grab_from_url(url, seek)
            if url
            else await _grab_from_attachment(attachment_id, seek, ctx)
        )
        if isinstance(frame, str):
            return frame  # a clean error — nothing stored

        origin = "that video" if url else "the attached video"
        prompt = f"frame from {url or 'attachment ' + attachment_id} @ {seek:g}s"
        try:
            row = await persist_chat_image(
                maker,
                ctx.session,
                blobs,
                repo,
                data=frame,
                provenance=PROVENANCE_FRAME,
                model="ffmpeg",
                prompt=prompt,
            )
        except (UndecodableImage, ImageTooLarge) as exc:
            return str(exc)

        image_id = str(row.id)
        caption = await _vision_read(frame, question) if question else ""
        where = f"at {seek:g}s of {origin}" if seek else f"from {origin}"
        summary = (
            f"Grabbed a still {where} (image_id {image_id}). Use analyze_image with"
            f" source_image_id {image_id} to look at it, or compare_images to compare it"
            " with another image."
        )
        if caption:
            summary += f"\n\nWhat it shows: {caption}"
        return ToolOutput(summary, view=chat_image_view(row) if show else None)

    return {"grab_frame": grab_frame_tool}
