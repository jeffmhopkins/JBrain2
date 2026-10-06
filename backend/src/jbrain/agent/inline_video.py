"""A short chat video, sent inside jerv's own context (docs/plans/NATIVE_VIDEO_PLAN.md §5).

When jerv's chat model takes video (Flash-Next), a clip whose charge fits
`INLINE_VIDEO_BUDGET_TOKENS` rides the conversation itself, the way an attached image does: it is
pinned at the turn it was attached (api.agent's anchor), so the engine's prefix cache holds it
and jerv answers — and follows up — from having seen it, not from a tool's summary. A longer clip
stays a reference that `analyze_video` reads in its own call.

The clip must be byte-identical on every turn that carries it, or the cache misses and the whole
clip is read again. So it is transcoded once, kept as a blob, and its record (blob id, length,
charge, rendered transcript) cached on the attachment row (`native_clip`, migration 0221); every
later turn re-reads that record. The transcript rides as text in the note because the model
cannot hear. A clip too long to inline is recorded too, so it is probed once, not every turn.
"""

import base64
from dataclasses import dataclass
from typing import Any

import structlog

from jbrain import media
from jbrain.agent.attachments import AttachmentInfo, TurnAttachmentRepo
from jbrain.db.session import SessionContext
from jbrain.ingest.video import NativeClipper, build_timeline, transcribe_audio
from jbrain.llm import LlmVideo, slot_roles
from jbrain.llm.local_gateway import LocalGateway
from jbrain.storage import BlobStore
from jbrain.transcribe import TranscribeClient

log = structlog.get_logger()

# The owner's ceiling (2026-10-06) on what inline video may cost jerv's context: every clip in
# view at once — this turn's and the recent turns' — shares it. About 31 s at the engine's 1 fps.
INLINE_VIDEO_BUDGET_TOKENS = 64 * 1024
# A clip whose transcode would be wasted (its charge cannot fit the budget) is never transcoded.
INLINE_MAX_SECONDS = (
    2 * (INLINE_VIDEO_BUDGET_TOKENS // slot_roles.IMAGE_TOKENS_CHARGE) - 1
) / slot_roles.VIDEO_FPS
# Refuse an oversized upload before reading it, as analyze_video does.
_MAX_SOURCE_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True)
class InlineClip:
    info: AttachmentInfo
    video: LlmVideo
    tokens: int
    seconds: float
    transcript: str | None  # the [mm:ss] timeline, "" for no speech, None when not transcribed


def clip_note(clips: list[InlineClip]) -> str:
    """The note beside the clips in their anchor message. Built only from the cached record, so
    it is byte-identical on every turn that carries the clips."""
    parts: list[str] = []
    for c in clips:
        if c.transcript is None:
            heard = "its audio was not transcribed"
        elif not c.transcript:
            heard = "it has no speech"
        else:
            heard = f"what is said in it:\n{c.transcript}"
        parts.append(
            f'[video "{c.info.filename}" (id {c.info.id}) — you can watch this clip here: '
            f"{c.seconds:.0f} s, sampled at about one frame per second with timestamps. Describe "
            "and reason about it yourself; do NOT call analyze_video for it. You cannot hear it; "
            f"{heard}]"
        )
    return "\n\n".join(parts)


async def default_inline_clipper(video: bytes) -> media.NativeClip:
    return await media.native_clip(video, max_seconds=INLINE_MAX_SECONDS, fps=slot_roles.VIDEO_FPS)


class InlineVideos:
    """Resolves a chat video to the clip jerv sees inline, transcoding and transcribing it at
    most once. Built only where ffmpeg can transcode; whisper is optional."""

    def __init__(
        self,
        repo: TurnAttachmentRepo,
        blobs: BlobStore,
        *,
        transcribe: TranscribeClient | None = None,
        transcribe_model: str = "",
        gateway: LocalGateway | None = None,
        clipper: NativeClipper = default_inline_clipper,
    ) -> None:
        self._repo = repo
        self._blobs = blobs
        self._transcribe = transcribe
        self._transcribe_model = transcribe_model
        self._gateway = gateway
        self._clipper = clipper

    async def clip(
        self, ctx: SessionContext, info: AttachmentInfo, *, budget: int
    ) -> InlineClip | None:
        """The inline clip for `info` when it fits `budget` tokens, else None (the video stays a
        reference). Never raises: a clip that cannot be read is simply not inlined."""
        try:
            record = await self._repo.native_clip(ctx, info.id)
            if record is None:
                record = await self._build(ctx, info)
        except Exception as exc:  # noqa: BLE001 - a chat turn must never fail on a clip
            log.warning("inline_video.failed", attachment=info.id, error=repr(exc))
            return None
        clip_id, tokens, seconds = (
            record.get("clip_id"),
            record.get("tokens"),
            record.get("seconds"),
        )
        if not clip_id or not isinstance(tokens, int) or tokens > budget or not seconds:
            return None
        try:
            data = await self._blobs.get(str(clip_id))
        except FileNotFoundError:
            return None
        return InlineClip(
            info=info,
            video=LlmVideo(
                media_type=media.NATIVE_VIDEO_MEDIA_TYPE,
                data=base64.b64encode(data).decode("ascii"),
                seconds=float(seconds),
            ),
            tokens=tokens,
            seconds=float(seconds),
            transcript=record.get("transcript"),
        )

    async def _build(self, ctx: SessionContext, info: AttachmentInfo) -> dict[str, Any]:
        record: dict[str, Any] = {"clip_id": None, "seconds": None, "tokens": None}
        if info.size_bytes <= _MAX_SOURCE_BYTES:
            raw = await self._blobs.get(info.sha256)
            try:
                clip = await self._clipper(raw)
            except media.TranscodeError as exc:
                log.warning("inline_video.transcode_failed", attachment=info.id, error=str(exc))
            else:
                record = await self._record(clip, raw, info)
        await self._repo.set_native_clip(ctx, info.id, record)
        return record

    async def _record(
        self, clip: media.NativeClip, raw: bytes, info: AttachmentInfo
    ) -> dict[str, Any]:
        tokens = slot_roles.video_tokens_charge(clip.seconds) if clip.seconds else None
        record: dict[str, Any] = {"clip_id": None, "seconds": clip.seconds, "tokens": tokens}
        if clip.data is None or tokens is None or tokens > INLINE_VIDEO_BUDGET_TOKENS:
            return record
        transcript: str | None = None
        if self._transcribe is not None:
            try:
                heard = await transcribe_audio(
                    self._transcribe,
                    self._gateway,
                    self._transcribe_model,
                    raw,
                    filename=info.filename,
                    media_type=info.media_type,
                )
            except Exception as exc:  # noqa: BLE001 - the clip is still worth seeing unheard
                log.info("inline_video.transcribe_failed", attachment=info.id, error=repr(exc))
            else:
                transcript = build_timeline([], list(heard["words"])) if heard else ""
        record.update(clip_id=await self._blobs.put(clip.data), transcript=transcript)
        return record
