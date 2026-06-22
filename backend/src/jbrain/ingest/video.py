"""The analyze_video_attachment job handler: one video attachment -> a cached
video-analysis row (map -> fuse -> reduce).

The video sibling of jbrain.ingest.ocr / jbrain.ingest.transcribe_job, and the
map-reduce-over-a-text-bottleneck the research converged on (docs/VIDEO_ANALYSIS_PLAN.md):

  MAP    sample bounded, deduped frames (jbrain.media, ffmpeg) and caption each via
         the vision model (router task `agent.vision`); transcribe the audio track
         via the existing whisper path (the gateway's ffmpeg pulls the audio).
  FUSE   interleave the frame captions and the spoken utterances on one [mm:ss]
         timeline so the reduce step reads what was shown *and* said, in order.
  REDUCE fold that timeline into one summary (router task `video.summarize`).

Persist a single kind='video_analysis' AttachmentExtract: `text` = the summary (so
it chunks and is searchable, kind-agnostic, exactly like ocr/caption/transcript),
the per-frame timeline + transcript in the `analysis` jsonb column, and each kept
frame's JPEG as a content-addressed blob whose id rides the timeline as `thumb_id`
(no URLs — invariant #9). Write-once delete+insert (the chunks pattern) keeps a
retry idempotent; the handler then re-enqueues ingest_note so the rebuilt chunks
pick the summary up.

Confidence is honest and capped ("Guards", docs/ANALYSIS.md): a video analysis is
machine-watched and machine-heard, not author-written, so it sits at the caption
ceiling — facts later mined from the summary inherit reduced confidence and can
never auto-supersede note text. An empty result (no decodable frames and no speech)
writes nothing, so the on-demand tool re-tries rather than caching a dead marker.

Like transcribe_attachment this is in-code only (NOT an app.actions seed row, so
migration 0035's seed-lockstep is untouched); the worker adds it to its
build_registry tuple. Frame sampling and whisper are best-effort: a clip ffmpeg
can't decode degrades to a transcript-only analysis, and an unconfigured whisper
degrades to a frames-only one.
"""

import asyncio
import base64
import re
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import media, queue
from jbrain.db.session import scoped_session
from jbrain.llm import LlmImage, LlmRouter
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.llm.promptfile import load_prompt
from jbrain.media import SampledFrame
from jbrain.models.notes import Attachment, AttachmentExtract, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore
from jbrain.transcribe import TranscribeClient
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

KIND_VIDEO_ANALYSIS = "video_analysis"

# In-code only (NOT an app.actions seed row — migration 0035's seed-lockstep is
# untouched), the sibling of transcribe_attachment: kicked on demand by the
# analyze_video tool (Wave 3), never by a seeded pipeline. The worker adds it to its
# build_registry tuple like the other post-Phase-4 actions.
VIDEO_ANALYSIS_SPEC = ActionSpec(
    name="analyze_video_attachment",
    version=1,
    handler="analyze_video_attachment",
    domain_optional=True,
    mutating=True,
    cost_class="expensive",
    dedup_key_expr="attachment_id",
    description="Analyze a video attachment: caption sampled frames, transcribe the"
    " audio, and summarize the fused timeline.",
)

# The Guards cap (docs/ANALYSIS.md): a video analysis is a model's reading of stills
# plus its hearing of the audio — it describes, it does not transcribe the author, so
# it sits at the caption ceiling, never above. The row carries this flat ceiling; the
# per-frame/per-word confidences live in `analysis` for the UI gradient.
VIDEO_ANALYSIS_CONFIDENCE = 0.6

# The vision frame-caption prompt and the reduce summary prompt are co-located
# .prompt artifacts (docs/DEVELOPMENT.md). Captioning routes by the `agent.vision`
# task (the same vision route jerv's analyze_image uses, so an on-box operator points
# both at local qwen3-vl); the summary routes by its own `video.summarize` task.
_FRAME = load_prompt(Path(__file__).parent / "prompts" / "video_frame.prompt")
_SUMMARY = load_prompt(Path(__file__).parent / "prompts" / "video_summary.prompt")
FRAME_SYSTEM = _FRAME.render()
SUMMARY_SYSTEM = _SUMMARY.render()
FRAME_MAX_TOKENS = int(_FRAME.config["max_tokens"])
SUMMARY_MAX_TOKENS = int(_SUMMARY.config["max_tokens"])

FRAME_CAPTION_TASK = "agent.vision"
SUMMARY_TASK = "video.summarize"

# Group transcript words into short utterances for the timeline: flush on a
# sentence end or once a line reaches this many words, so a long monologue becomes
# a handful of timestamped lines rather than one wall of text or one line per word.
_MAX_UTTERANCE_WORDS = 14
_SENTENCE_END = re.compile(r"[.!?]\"?$")


def _mmss(ms: int) -> str:
    total = max(0, ms) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def group_utterances(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold per-word transcript data into timestamped utterances [{t_ms, text}].

    Each utterance carries the start of its first word and runs until a sentence end
    or `_MAX_UTTERANCE_WORDS`, whichever comes first — enough granularity to align
    speech with the frames on the timeline without drowning the reduce prompt."""
    utterances: list[dict[str, Any]] = []
    buf: list[str] = []
    start: int | None = None
    for w in words:
        token = str(w.get("text", "")).strip()
        if not token:
            continue
        if start is None:
            start = int(w.get("start_ms", 0))
        buf.append(token)
        if _SENTENCE_END.search(token) or len(buf) >= _MAX_UTTERANCE_WORDS:
            utterances.append({"t_ms": start, "text": " ".join(buf)})
            buf, start = [], None
    if buf and start is not None:
        utterances.append({"t_ms": start, "text": " ".join(buf)})
    return utterances


def build_timeline(frames: list[dict[str, Any]], words: list[dict[str, Any]]) -> str:
    """Interleave frame captions and spoken utterances into one [mm:ss] timeline.

    Frames and speech are merged and sorted by timestamp (frames before speech at the
    same instant — what's on screen frames the line that's said), then rendered one
    entry per line. This fused text is the reduce step's whole input."""
    entries: list[tuple[int, int, str]] = []
    for f in frames:
        entries.append((int(f["t_ms"]), 0, f"[{_mmss(int(f['t_ms']))}] (frame) {f['caption']}"))
    for u in group_utterances(words):
        entries.append((int(u["t_ms"]), 1, f"[{_mmss(int(u['t_ms']))}] (said) “{u['text']}”"))
    entries.sort(key=lambda e: (e[0], e[1]))
    return "\n".join(line for _, _, line in entries)


class VideoSampler(Protocol):
    """The frame sampler the handler runs off the event loop (faked in tests)."""

    def __call__(self, video: bytes) -> list[SampledFrame]: ...


class VideoPipeline:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        blobs: BlobStore,
        router: LlmRouter,
        *,
        transcribe: TranscribeClient | None = None,
        transcribe_model: str = "",
        gateway: LocalGateway | None = None,
        sampler: VideoSampler | None = None,
    ):
        self._maker = maker
        self._blobs = blobs
        self._router = router
        self._transcribe = transcribe
        self._transcribe_model = transcribe_model
        self._gateway = gateway
        # media.sample_frames is blocking (ffmpeg subprocess); the handler runs it via
        # asyncio.to_thread. Injectable so tests need no ffmpeg.
        self._sampler: VideoSampler = sampler or media.sample_frames

    async def analyze_video_attachment(self, payload: dict[str, Any]) -> None:
        """Handle an analyze_video_attachment job: {attachment_id}; gone rows no-op."""
        attachment_id = str(payload["attachment_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            att = (
                await session.execute(select(Attachment).where(Attachment.id == attachment_id))
            ).scalar_one_or_none()
            if att is None:
                log.info("video.skipped", attachment_id=attachment_id, reason="attachment gone")
                return
            note = (
                await session.execute(select(Note).where(Note.id == att.note_id))
            ).scalar_one_or_none()
            if note is None or note.deleted_at is not None:
                log.info("video.skipped", attachment_id=attachment_id, reason="note gone")
                return
            note_id = str(att.note_id)
            sha256, media_type, filename, domain = (
                att.sha256,
                att.media_type,
                att.filename,
                att.domain_code,
            )
            # Re-analysis runs only when the cache row is missing: a re-ingest of an
            # already-analyzed note must not re-bill the vision + whisper models.
            has_analysis = (
                await session.execute(
                    select(AttachmentExtract.id)
                    .where(
                        AttachmentExtract.attachment_id == attachment_id,
                        AttachmentExtract.kind == KIND_VIDEO_ANALYSIS,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none() is not None
            attachment_uuid = att.id

        if has_analysis:
            log.info("video.skipped", attachment_id=attachment_id, reason="already cached")
            return

        data = await self._blobs.get(sha256)
        # MAP — sample frames (off the event loop) and caption each.
        frames = await asyncio.to_thread(self._sampler, data)
        captioned: list[dict[str, Any]] = []
        for frame in frames:
            image = LlmImage(
                media_type="image/jpeg", data=base64.b64encode(frame.jpeg).decode("ascii")
            )
            caption = await self._router.complete(
                FRAME_CAPTION_TASK,
                system=FRAME_SYSTEM,
                user_text=f"Caption this frame from the video (file: {filename}).",
                images=[image],
                max_tokens=FRAME_MAX_TOKENS,
            )
            thumb_id = await self._blobs.put(frame.jpeg)
            captioned.append(
                {"t_ms": frame.timestamp_ms, "caption": caption.text.strip(), "thumb_id": thumb_id}
            )

        # MAP — transcribe the audio track (best-effort; absent when whisper is off).
        transcript = await self._transcribe_audio(data, filename=filename, media_type=media_type)
        words = list(transcript["words"]) if transcript else []

        if not captioned and not transcript:
            # Nothing decodable and nothing spoken: write no marker so the on-demand
            # tool re-tries (e.g. once ffmpeg/whisper is configured) rather than
            # caching a dead empty analysis.
            log.info("video.skipped", attachment_id=attachment_id, reason="no frames or audio")
            return

        # FUSE + REDUCE — one timeline, one summary.
        timeline = build_timeline(captioned, words)
        summary = await self._router.complete(
            SUMMARY_TASK,
            system=SUMMARY_SYSTEM,
            user_text=timeline,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        summary_text = summary.text.strip()

        duration_ms = self._duration_ms(captioned, transcript)
        analysis = {
            "duration_ms": duration_ms,
            "frames": captioned,
            "transcript": transcript,
        }
        row = AttachmentExtract(
            attachment_id=attachment_uuid,
            kind=KIND_VIDEO_ANALYSIS,
            tool=":".join(await self._router.effective_spec(FRAME_CAPTION_TASK)),
            text=summary_text,
            confidence=VIDEO_ANALYSIS_CONFIDENCE if summary_text else 0.0,
            analysis=analysis,
            source_anchor=filename,
            domain_code=domain,
        )
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await session.execute(
                delete(AttachmentExtract).where(
                    AttachmentExtract.attachment_id == attachment_id,
                    AttachmentExtract.kind == KIND_VIDEO_ANALYSIS,
                )
            )
            session.add(row)
        # Rebuild chunks so the summary becomes searchable (and the analysis that
        # follows ingest sees it).
        await queue.enqueue(self._maker, SYSTEM_CTX, "ingest_note", {"note_id": note_id})
        log.info(
            "video.analyzed",
            attachment_id=attachment_id,
            note_id=note_id,
            frames=len(captioned),
            has_audio=transcript is not None,
            summary_chars=len(summary_text),
        )

    async def _transcribe_audio(
        self, data: bytes, *, filename: str, media_type: str
    ) -> dict[str, Any] | None:
        """The fused transcript dict, or None when whisper is unconfigured or the clip
        has no speech. The whisper gateway's ffmpeg extracts the audio track from the
        video container, so the raw video bytes ride the existing transcribe path."""
        if self._transcribe is None:
            return None
        try:
            result = await self._transcribe.transcribe(
                data, filename=filename, media_type=media_type
            )
        finally:
            await self._unload()
        words = [
            {
                "text": w.text,
                "start_ms": w.start_ms,
                "end_ms": w.end_ms,
                "confidence": round(w.confidence, 4),
            }
            for w in result.words
        ]
        clean = result.text.strip()
        if not clean and not words:
            return None  # silent / non-speech audio — no transcript to fuse
        return {"text": clean, "words": words, "duration_ms": result.duration_ms}

    @staticmethod
    def _duration_ms(frames: list[dict[str, Any]], transcript: dict[str, Any] | None) -> int | None:
        """Best-effort clip length: the transcript's probed duration, else the last
        sampled frame's offset (the sampler clamps it to the clip)."""
        if transcript and transcript.get("duration_ms"):
            return int(transcript["duration_ms"])
        if frames:
            return int(frames[-1]["t_ms"])
        return None

    async def _unload(self) -> None:
        """Best-effort eviction of the whisper model from the gateway (load-on-demand
        / unload-after), mirroring transcribe_attachment. Never raises: freeing VRAM
        is an optimization, and the gateway TTL-unloads anyway."""
        if self._gateway is None or not self._transcribe_model:
            return
        try:
            await self._gateway.unload(self._transcribe_model)
        except LocalGatewayError as exc:
            log.info("video.unload_failed", model=self._transcribe_model, error=str(exc))
