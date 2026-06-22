"""The `transcribe` agent tool: jerv reads an attached audio file with the local
whisper model (docs/WHISPER_TRANSCRIPTION_PLAN.md).

The audio sibling of `analyze_image`: resolves a chat attachment by id under the
session's RLS scope (a foreign/out-of-scope id reads as a clean miss, never a
leak), fetches its bytes, and delegates to the whisper gateway. Wired only when
the whisper backend is configured; the registry drops the sidecar otherwise
(graceful degrade, like the image tools). The model is freed after each call
(load-on-demand / unload-after), best-effort — VRAM hygiene, not correctness.
"""

import uuid

import structlog

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.storage import BlobStore
from jbrain.transcribe import TranscribeClient, Transcript

log = structlog.get_logger()

_NO_AUDIO = "No attached audio with that id is in this chat."
# The chat tool's size ceiling, the ingest budget's sibling (config.whisper_max_bytes,
# DEFAULT_TRANSCRIBE_MAX_BYTES): refuse an oversized clip up front rather than block on
# a multi-minute call that the gateway timeout would kill anyway.
DEFAULT_TOOL_MAX_BYTES = 100 * 1024 * 1024


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def build_transcribe_handlers(
    client: TranscribeClient,
    blobs: BlobStore,
    attachments: TurnAttachmentRepo,
    model: str,
    *,
    gateway: LocalGateway | None = None,
    max_bytes: int = DEFAULT_TOOL_MAX_BYTES,
) -> dict[str, ToolHandler]:
    """The `transcribe` handler, bound to its services. `model` is the served name
    the gateway unloads after the call; `gateway` is optional (no unload without
    it, the same best-effort posture as the image tools); `max_bytes` caps the clip."""

    async def transcribe_tool(arguments: dict, ctx: ToolContext) -> str:
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        # A chat attachment is domain-scoped, so it is read under the session's
        # attachment context (its scopes + the file's stamped domain), the same
        # widening the chat turn uses; RLS still hides a foreign id as a clean miss.
        if ctx.agent_session_id is None or not _is_uuid(attachment_id):
            return _NO_AUDIO
        att_ctx = await attachments.session_read_context(ctx.session, ctx.agent_session_id)
        if att_ctx is None:
            return _NO_AUDIO
        info = await attachments.get(att_ctx, attachment_id)
        if info is None:
            return _NO_AUDIO
        if not info.media_type.startswith("audio/"):
            return "That attachment isn't audio — transcribe only reads audio files."
        if info.size_bytes > max_bytes:
            return "That audio file is too large to transcribe."
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            return "That audio file is no longer available."
        try:
            transcript = await client.transcribe(
                data, filename=info.filename, media_type=info.media_type
            )
        except Exception as exc:  # noqa: BLE001 - a tool error is a recoverable observation
            log.warning("transcribe_tool_failed", error=repr(exc))
            return "I couldn't transcribe that audio right now — the speech model didn't respond."
        finally:
            await _unload(gateway, model)

        text = transcript.text.strip()
        if not text:
            return f'No speech was found in "{info.filename}".'
        # The model reads the transcript text; the owner sees the rich card (audio
        # player + per-word confidence + sync). The view carries the attachment id,
        # not a URL — the component builds the audio src (invariant #9).
        return ToolOutput(
            f'Transcript of "{info.filename}":\n{text}',
            view=_transcript_view(attachment_id, info.filename, model, transcript),
        )

    return {"transcribe": transcribe_tool}


def _transcript_view(
    attachment_id: str, filename: str, model: str, transcript: Transcript
) -> ViewPayload:
    return ViewPayload(
        view="transcript",
        surface="inline",
        data={
            # `source` tells the component which download endpoint to build the
            # audio src from (a chat attachment here vs a note attachment).
            "attachment_id": attachment_id,
            "source": "chat",
            "filename": filename,
            "model": model,
            "duration_ms": transcript.duration_ms,
            "words": [
                {
                    "text": w.text,
                    "start_ms": w.start_ms,
                    "end_ms": w.end_ms,
                    "confidence": round(w.confidence, 4),
                }
                for w in transcript.words
            ],
        },
    )


async def _unload(gateway: LocalGateway | None, model: str) -> None:
    """Free the model from the gateway after a call (load-on-demand / unload-after).
    Never raises: the gateway TTL-unloads anyway if this can't reach it."""
    if gateway is None:
        return
    try:
        await gateway.unload(model)
    except LocalGatewayError as exc:
        log.info("transcribe_tool.unload_failed", model=model, error=str(exc))
