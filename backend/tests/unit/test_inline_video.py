"""Inline video (NATIVE_VIDEO_PLAN §5): the clip cache that keeps a short video byte-identical
across turns, the shared token budget, the adapter's `input_video` parts on a conversation, and
the attachment notes. Pure unit tests — faked repo, blobs, clipper and whisper (no ffmpeg)."""

import pytest

from jbrain import media
from jbrain.agent.attachment_content import build_attachment_content
from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.inline_video import (
    INLINE_MAX_SECONDS,
    INLINE_VIDEO_BUDGET_TOKENS,
    InlineVideos,
    clip_note,
)
from jbrain.db.session import SessionContext
from jbrain.llm import LlmVideo, UserMessage, slot_roles
from jbrain.llm.anthropic import _anthropic_message
from jbrain.llm.errors import LlmVideoUnsupportedError
from jbrain.llm.openai_compat import OpenAiCompatClient, _openai_messages
from jbrain.transcribe import Transcript, Word

CTX = SessionContext(principal_kind="owner")
VID = AttachmentInfo("v1", "clip.mp4", "video/mp4", 10, "sha-raw", "general")


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {"sha-raw": b"raw video"}

    async def put(self, data: bytes) -> str:
        key = f"sha-{len(self.data)}"
        self.data[key] = data
        return key

    async def get(self, sha256: str) -> bytes:
        try:
            return self.data[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc


class FakeRepo:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.writes = 0
        self.rows: dict[str, AttachmentInfo] = {VID.id: VID}

    async def native_clip(self, ctx: SessionContext, attachment_id: str) -> dict | None:
        return self.records.get(attachment_id)

    async def set_native_clip(self, ctx: SessionContext, attachment_id: str, record: dict) -> None:
        self.writes += 1
        self.records[attachment_id] = record

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        return self.rows.get(attachment_id)


class FakeWhisper:
    def __init__(self, words: tuple[Word, ...] = (Word("four", 1000, 1400, 0.9),)) -> None:
        self.calls = 0
        self.words = words

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls += 1
        return Transcript(text=" ".join(w.text for w in self.words), words=self.words)


def _service(seconds: float | None, *, whisper: FakeWhisper | None = None, fail: bool = False):
    calls: list[bytes] = []

    async def clipper(video: bytes) -> media.NativeClip:
        calls.append(video)
        if fail:
            raise media.TranscodeError("ffmpeg exited 1")
        fits = seconds is not None and seconds <= INLINE_MAX_SECONDS
        return media.NativeClip(seconds=seconds, data=b"MKV" if fits else None)

    repo, blobs = FakeRepo(), FakeBlobs()
    service = InlineVideos(repo, blobs, transcribe=whisper, clipper=clipper)  # type: ignore[arg-type]
    return service, repo, blobs, calls


def test_the_budget_is_about_half_a_minute() -> None:
    assert INLINE_VIDEO_BUDGET_TOKENS == 65_536 and INLINE_MAX_SECONDS == 31.0
    assert slot_roles.video_tokens_charge(INLINE_MAX_SECONDS) <= INLINE_VIDEO_BUDGET_TOKENS
    assert slot_roles.video_tokens_charge(INLINE_MAX_SECONDS + 1) > INLINE_VIDEO_BUDGET_TOKENS


async def test_a_short_clip_is_transcoded_and_transcribed_once() -> None:
    whisper = FakeWhisper()
    service, repo, blobs, calls = _service(7.4, whisper=whisper)

    first = await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS)
    second = await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS)

    assert first is not None and second is not None
    assert first == second  # byte-identical on every turn, or the prefix cache misses
    assert len(calls) == 1 and whisper.calls == 1 and repo.writes == 1
    assert first.video.media_type == media.NATIVE_VIDEO_MEDIA_TYPE
    assert first.video.seconds == 7.4 and first.tokens == slot_roles.video_tokens_charge(7.4)
    assert first.transcript == "[00:01] (said) “four”"
    assert blobs.data[repo.records["v1"]["clip_id"]] == b"MKV"


async def test_a_clip_over_the_budget_given_is_not_inlined() -> None:
    service, _, _, _ = _service(7.4)
    assert await service.clip(CTX, VID, budget=10_000) is None
    # The record survives; a later turn with room still gets it.
    assert await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS) is not None


async def test_a_long_clip_is_probed_once_and_never_inlined() -> None:
    service, repo, _, calls = _service(45.0)
    assert await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS) is None
    assert await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS) is None
    assert len(calls) == 1
    assert repo.records["v1"]["clip_id"] is None and repo.records["v1"]["seconds"] == 45.0


async def test_a_failed_transcode_is_recorded_and_not_retried() -> None:
    service, repo, _, calls = _service(7.0, fail=True)
    assert await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS) is None
    assert await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS) is None
    assert len(calls) == 1 and repo.records["v1"]["clip_id"] is None


async def test_a_lost_clip_blob_reads_as_not_inlined() -> None:
    service, repo, blobs, _ = _service(7.0)
    await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS)
    del blobs.data[repo.records["v1"]["clip_id"]]
    assert await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS) is None


async def test_no_whisper_and_no_speech_are_told_apart() -> None:
    unheard, _, _, _ = _service(7.0)
    silent, _, _, _ = _service(7.0, whisper=FakeWhisper(words=()))
    a = await unheard.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS)
    b = await silent.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS)
    assert a is not None and b is not None
    assert "its audio was not transcribed" in clip_note([a])
    assert "it has no speech" in clip_note([b])


async def test_the_note_names_the_clip_and_forbids_the_tool() -> None:
    service, _, _, _ = _service(7.4, whisper=FakeWhisper())
    clip = await service.clip(CTX, VID, budget=INLINE_VIDEO_BUDGET_TOKENS)
    assert clip is not None
    note = clip_note([clip])
    assert note.startswith('[video "clip.mp4" (id v1) — you can watch this clip here: 7 s')
    assert "do NOT call analyze_video" in note and "[00:01] (said) “four”" in note


# --- the adapter ---------------------------------------------------------------------------


def test_a_conversation_message_carries_its_clip_as_input_video() -> None:
    clip = LlmVideo(media_type="video/x-matroska", data="QUJD", seconds=7.0)
    out = _openai_messages("sys", [UserMessage(text="see", videos=[clip])])
    assert out[1]["content"] == [
        {"type": "input_video", "input_video": {"data": "QUJD"}},
        {"type": "text", "text": "see"},
    ]


def test_cloud_adapters_refuse_a_clip() -> None:
    message = UserMessage(text="see", videos=[LlmVideo("video/x-matroska", "QUJD")])
    with pytest.raises(LlmVideoUnsupportedError):
        _anthropic_message(message)
    xai = OpenAiCompatClient("https://api.x.ai/v1", "k", provider="xai")
    with pytest.raises(LlmVideoUnsupportedError):
        xai._converse_payload(
            model="grok-4.3", system="s", messages=[message], tools=(), max_tokens=10
        )


def test_the_slot_charge_counts_every_clip_in_the_conversation() -> None:
    a = LlmVideo("video/x-matroska", "x", seconds=7.0)
    b = LlmVideo("video/x-matroska", "y", seconds=20.0)
    messages = [UserMessage(text="1", videos=[a]), UserMessage(text="2", videos=[b])]
    expected = slot_roles.video_tokens_charge(7.0) + slot_roles.video_tokens_charge(20.0)
    assert slot_roles.message_video_tokens(messages) == expected


# --- the attachment notes ------------------------------------------------------------------


async def test_an_inlined_video_gets_no_note_and_a_long_one_points_at_analyze_video() -> None:
    repo = FakeRepo()
    repo.rows["v2"] = AttachmentInfo("v2", "long.mp4", "video/mp4", 10, "sha-raw", "general")
    content = await build_attachment_content(
        repo,  # type: ignore[arg-type]
        FakeBlobs(),  # type: ignore[arg-type]
        CTX,
        ["v1", "v2"],
        transcribe_enabled=True,
        video_enabled=True,
        inline_video_ids=frozenset({"v1"}),
    )
    assert [i.id for i in content.video_infos] == ["v1", "v2"]
    assert [i.id for i in content.media_infos] == ["v1", "v2"]
    assert "clip.mp4" not in content.extra_text
    assert 'attached video "long.mp4"' in content.extra_text
    assert "analyze_video" in content.extra_text and "transcribe tool" in content.extra_text
