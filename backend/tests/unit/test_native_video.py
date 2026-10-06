"""Native video on Flash-Next (docs/plans/NATIVE_VIDEO_PLAN.md V1 + V2): the gate, the 60 s
split, every fallback to the frame pipeline, the follow-up question that re-sends the stored
clip, and the stream path. Pure unit tests — faked LLM clients, a canned sampler and clipper
(no ffmpeg), an in-memory blob store."""

from typing import Any

from jbrain import media
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.videotools import build_video_handlers
from jbrain.db.session import SessionContext
from jbrain.ingest import stream_analysis
from jbrain.ingest.video import (
    NATIVE_SYSTEM,
    PATH_FRAMES,
    PATH_NATIVE,
    SUMMARY_REQUEST,
    run_video_analysis,
)
from jbrain.llm import FakeLlmClient, LlmRouter, LlmVideo, local_catalog, slot_roles
from jbrain.llm import engine as engines
from jbrain.llm.errors import LlmTransientError
from jbrain.media import NativeClip, SampledFrame, TranscodeError
from jbrain.stream import ResolvedStream, StreamSample
from jbrain.transcribe import Transcript, Word

FLASH = ("local", "qwen3.8-flash-next")
SESSION = "11111111-1111-1111-1111-111111111111"
ATT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CTX = ToolContext(
    session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=SESSION
)
FRAMES = [SampledFrame(0, b"\xff\xd8f0"), SampledFrame(5000, b"\xff\xd8f1")]


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        key = f"sha-{len(self.data)}-{len(data)}"
        self.data[key] = data
        return key

    async def get(self, sha256: str) -> bytes:
        try:
            return self.data[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc


class VideoFailingClient(FakeLlmClient):
    """Fails any call carrying a video, as an engine without ffmpeg (or a dropped connection)
    would; still writes the frame pipeline's text summary."""

    def __init__(self) -> None:
        super().__init__(["a frames summary"])

    async def complete(self, **kw: Any):  # type: ignore[override]
        if kw.get("videos"):
            self.calls.append({"videos": list(kw["videos"]), "failed": True})
            raise LlmTransientError("local: network error: ConnectError")
        return await super().complete(**kw)


class FakeTranscribe:
    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        return Transcript(
            text="Here are the numbers.",
            words=(Word("Here", 1000, 1300, 0.9), Word("numbers.", 1300, 1800, 0.9)),
            duration_ms=12000,
        )


def _router(
    local: FakeLlmClient, *, video_spec: tuple[str, str] = FLASH, engine: str | None = None
) -> tuple[LlmRouter, FakeLlmClient]:
    xai = FakeLlmClient(["a caption"])

    async def _engine() -> engines.Engine:
        return engine  # type: ignore[return-value]

    router = LlmRouter(
        {"xai": xai, "local": local},
        {"agent.vision": ("xai", "grok-4.3"), "video.summarize": video_spec},
        engine_loader=_engine if engine else None,
    )
    return router, xai


def _sampler(frames: list[SampledFrame] = FRAMES):
    async def sample(video: bytes) -> list[SampledFrame]:
        return list(frames)

    return sample


def _clipper(seconds: float | None, data: bytes | None = b"MKV", calls: list | None = None):
    async def clip(video: bytes) -> NativeClip:
        if calls is not None:
            calls.append(video)
        return NativeClip(seconds=seconds, data=data if seconds and seconds <= 60 else None)

    return clip


async def _run(router: LlmRouter, blobs: FakeBlobs, clipper, **kw: Any):
    return await run_video_analysis(
        b"raw video",
        filename="clip.mp4",
        media_type="video/mp4",
        router=router,
        blobs=blobs,  # type: ignore[arg-type]
        sampler=_sampler(),
        clipper=clipper,
        **kw,
    )


# --- the gate ------------------------------------------------------------------------------


def test_only_flash_next_declares_video() -> None:
    with_video = [m.id for m in local_catalog.CATALOG if m.supports_video]
    assert with_video == ["qwen3.8-flash-next"]
    assert local_catalog.supports_video("qwen3.8-flash-next")
    assert not local_catalog.supports_video("qwen3.8-27b")
    assert not local_catalog.supports_video("not-in-the-catalog")


async def test_gate_keys_on_the_model_not_the_provider() -> None:
    assert await _router(FakeLlmClient())[0].supports_video("video.summarize")
    # The Standard engine is "local" too, and its models have no video path.
    standard, _ = _router(FakeLlmClient(), video_spec=("local", "qwen3.8-27b"))
    assert not await standard.supports_video("video.summarize")
    cloud, _ = _router(FakeLlmClient(), video_spec=("xai", "grok-4.3"))
    assert not await cloud.supports_video("video.summarize")


async def test_gate_follows_the_engine_remap() -> None:
    # A Standard pick runs on Flash-Next while Flash-Next serves, so it gets the native path.
    router, _ = _router(FakeLlmClient(), video_spec=("local", "qwen3.8-27b"), engine="flash-next")
    assert await router.supports_video("video.summarize")


def test_a_minute_fits_the_native_slot_with_room_to_answer() -> None:
    pool = slot_roles.FLASH_NEXT_POOL
    prompt = slot_roles.estimate_prompt_tokens(
        "qwen3.8-flash-next", chars=20_000, video_tokens=slot_roles.video_tokens_charge(60.0)
    )
    admission = slot_roles.admit(
        pool, slot_roles.NATIVE_VIDEO_ROLE, prompt_tokens=prompt, max_tokens=4096
    )
    assert not admission.clamped


# --- run_video_analysis --------------------------------------------------------------------


async def test_short_clip_is_watched_natively() -> None:
    local = FakeLlmClient(["You hold up 4, 7 and 2."])
    router, xai = _router(local)
    blobs = FakeBlobs()

    result = await _run(router, blobs, _clipper(12.0), transcribe=FakeTranscribe(), keep_clip=True)

    assert result is not None and result.summary == "You hold up 4, 7 and 2."
    assert xai.calls == []  # no frame captioned, no text reduce
    (call,) = local.calls
    (video,) = call["videos"]
    assert isinstance(video, LlmVideo)
    assert video.media_type == media.NATIVE_VIDEO_MEDIA_TYPE and video.seconds == 12.0
    assert call["system"] == NATIVE_SYSTEM
    assert call["id_slot"] == slot_roles.FLASH_NEXT_POOL.slot(slot_roles.NATIVE_VIDEO_ROLE)
    assert "[00:01] (said) “Here numbers.”" in call["user_text"]
    assert call["user_text"].endswith(f"Request: {SUMMARY_REQUEST}")
    a = result.analysis
    assert a["path"] == PATH_NATIVE and a["fallback"] == ""
    assert a["duration_ms"] == 12000 and a["native_seconds"] == 12.0
    assert [f["caption"] for f in a["frames"]] == ["", ""]
    assert all(blobs.data[f["thumb_id"]] for f in a["frames"])
    assert blobs.data[a["native_clip_id"]] == b"MKV"
    assert result.tool == "local:qwen3.8-flash-next"


def _no_video(client: FakeLlmClient) -> bool:
    return all(not c.get("videos") for c in client.calls)


async def test_the_clip_is_kept_only_when_asked() -> None:
    blobs = FakeBlobs()
    router, _ = _router(FakeLlmClient(["A summary."]))
    result = await _run(router, blobs, _clipper(12.0))
    assert result is not None and result.analysis["native_clip_id"] is None
    assert b"MKV" not in blobs.data.values()


async def test_long_clip_reads_frames() -> None:
    local = FakeLlmClient(["a frames summary"])
    router, xai = _router(local)
    result = await _run(router, FakeBlobs(), _clipper(90.0))
    assert result is not None and result.summary == "a frames summary"
    # Two captions, then the text reduce — on the same routed model, with no clip.
    assert len(xai.calls) == 2 and len(local.calls) == 1 and _no_video(local)
    assert result.analysis["path"] == PATH_FRAMES
    assert result.analysis["fallback"] == "longer than 60 s"


async def test_unknown_length_reads_frames() -> None:
    router, _ = _router(FakeLlmClient())
    result = await _run(router, FakeBlobs(), _clipper(None))
    assert result is not None and result.analysis["fallback"] == "unknown length"


async def test_no_video_model_never_probes() -> None:
    calls: list = []
    router, _ = _router(FakeLlmClient(), video_spec=("xai", "grok-4.3"))
    result = await _run(router, FakeBlobs(), _clipper(10.0, calls=calls))
    assert calls == []
    assert result is not None and result.analysis["fallback"] == "model has no video input"


async def test_transcode_failure_falls_back() -> None:
    async def broken(video: bytes) -> NativeClip:
        raise TranscodeError("ffmpeg exited 1")

    local = FakeLlmClient()
    router, _ = _router(local)
    result = await _run(router, FakeBlobs(), broken)
    assert _no_video(local)
    assert result is not None and result.analysis["fallback"] == "transcode failed"


async def test_native_call_failure_falls_back_in_the_same_call() -> None:
    local = VideoFailingClient()
    router, xai = _router(local)
    result = await _run(router, FakeBlobs(), _clipper(10.0), transcribe=FakeTranscribe())
    assert result is not None and result.summary == "a frames summary"
    assert [c.get("failed") for c in local.calls] == [True, None]
    assert len(xai.calls) == 2
    assert result.analysis["fallback"] == "native call failed: LlmTransientError"
    assert result.analysis["transcript"]["text"] == "Here are the numbers."


async def test_an_empty_native_answer_falls_back() -> None:
    router, _ = _router(FakeLlmClient(["  ", "a frames summary"]))
    result = await _run(router, FakeBlobs(), _clipper(10.0))
    assert result is not None and result.summary == "a frames summary"
    assert result.analysis["fallback"] == "native call failed: LlmBadResponseError"


# --- the analyze_video tool's question (V2) ------------------------------------------------


class FakeAttachments:
    def __init__(self) -> None:
        from jbrain.agent.attachments import AttachmentInfo

        self.rows = {
            ATT: AttachmentInfo(
                id=ATT,
                filename="PXL.mp4",
                media_type="video/mp4",
                size_bytes=10,
                sha256="vid",
                domain_code="general",
            )
        }
        self.cache: dict[str, dict] = {}

    async def session_read_context(self, ctx: SessionContext, sid: str) -> SessionContext | None:
        return ctx if sid == SESSION else None

    async def get(self, ctx: SessionContext, attachment_id: str):  # type: ignore[no-untyped-def]
        return self.rows.get(attachment_id)

    async def analysis(self, ctx: SessionContext, attachment_id: str) -> dict | None:
        return self.cache.get(attachment_id)

    async def set_analysis(self, ctx: SessionContext, attachment_id: str, analysis: dict) -> None:
        self.cache[attachment_id] = analysis


def _tool(router: LlmRouter, clip_seconds: float = 12.0):
    blobs = FakeBlobs()
    blobs.data["vid"] = b"raw video"
    attachments = FakeAttachments()
    handlers = build_video_handlers(
        blobs,  # type: ignore[arg-type]
        attachments,  # type: ignore[arg-type]
        router,
        sampler=_sampler(),
        clipper=_clipper(clip_seconds),
    )
    return handlers["analyze_video"], attachments


async def test_question_is_answered_from_the_same_clip_prefix() -> None:
    local = FakeLlmClient(["A person holds up cards.", "4, 7 and 2."])
    router, _ = _router(local)
    tool, attachments = _tool(router)

    out = await tool({"source_attachment_id": ATT, "question": "What numbers?"}, CTX)

    summary_call, answer_call = local.calls
    assert summary_call["system"] == answer_call["system"]
    assert summary_call["videos"] == answer_call["videos"]
    # Everything before the request is shared, so the engine re-reads only the question.
    shared = summary_call["user_text"].rsplit("Request:", 1)[0]
    assert answer_call["user_text"] == f"{shared}Request: What numbers?"
    assert "A person holds up cards." in str(out)
    assert 'Answer to "What numbers?", from watching the clip:\n4, 7 and 2.' in str(out)
    assert isinstance(out, ToolOutput) and out.result_brief == "watched the clip"
    assert attachments.cache[ATT]["path"] == PATH_NATIVE


async def test_follow_up_resends_the_stored_clip_without_rerunning() -> None:
    local = FakeLlmClient(["A person holds up cards.", "4, 7 and 2.", "Left hand."])
    router, _ = _router(local)
    tool, _ = _tool(router)
    await tool({"source_attachment_id": ATT}, CTX)

    out = await tool({"source_attachment_id": ATT, "question": "Which hand?"}, CTX)

    assert len(local.calls) == 2  # the summary, then only the follow-up
    assert local.calls[1]["user_text"].endswith("Request: Which hand?")
    assert local.calls[1]["videos"] == local.calls[0]["videos"]
    assert "from watching the clip:\n4, 7 and 2." in str(out)
    assert isinstance(out, ToolOutput) and out.result_brief == "already analyzed"


async def test_question_on_a_frames_read_adds_no_call() -> None:
    local = FakeLlmClient(["a frames summary"])
    router, xai = _router(local)
    tool, _ = _tool(router, clip_seconds=120.0)
    out = await tool({"source_attachment_id": ATT, "question": "What numbers?"}, CTX)
    assert len(local.calls) == 1 and _no_video(local) and len(xai.calls) == 2
    assert isinstance(out, ToolOutput) and out.result_brief == "2 frames"
    assert "Answer to" not in str(out)


async def test_a_failed_answer_still_returns_the_summary() -> None:
    class AnswerFails(FakeLlmClient):
        async def complete(self, **kw: Any):  # type: ignore[override]
            if "What numbers?" in kw["user_text"]:
                raise LlmTransientError("boom")
            return await super().complete(**kw)

    router, _ = _router(AnswerFails(["A person holds up cards."]))
    tool, _ = _tool(router)
    out = await tool({"source_attachment_id": ATT, "question": "What numbers?"}, CTX)
    assert str(out) == 'Analysis of "PXL.mp4":\nA person holds up cards.'


# --- analyze_stream full mode (V2) ---------------------------------------------------------


def _vod(duration: float | None, *, live: bool = False) -> ResolvedStream:
    return ResolvedStream(
        media_url="https://cdn.example.com/v.mp4",
        title="Short",
        is_live=live,
        duration_s=duration,
        webpage_url="https://youtube.com/watch?v=abc",
    )


async def _stream(router: LlmRouter, resolved: ResolvedStream, mode: str = "full"):
    clipped: list[ResolvedStream] = []

    async def clipper(resolved: ResolvedStream) -> bytes:
        clipped.append(resolved)
        return b"MKV"

    async def sampler(resolved: ResolvedStream, **kw: Any) -> StreamSample:
        return StreamSample(frames=list(FRAMES))

    out = await stream_analysis.run_stream_pipeline(
        resolved,
        mode,
        {},
        want_transcript=False,
        router=router,
        blobs=FakeBlobs(),  # type: ignore[arg-type]
        transcribe=None,
        gateway=None,
        transcribe_model="",
        window_sampler=sampler,
        full_sampler=sampler,
        stream_clipper=clipper,
    )
    return out, clipped


async def test_short_full_video_is_watched_natively() -> None:
    local = FakeLlmClient(["A rocket lifts off."])
    router, xai = _router(local)
    out, clipped = await _stream(router, _vod(45.0))
    assert out is not None
    result, frames, _ = out
    assert result.summary == "A rocket lifts off." and result.analysis["path"] == PATH_NATIVE
    assert len(frames) == 2 and xai.calls == [] and len(clipped) == 1
    assert local.calls[0]["videos"][0].seconds == 45.0


async def test_long_or_windowed_streams_read_frames() -> None:
    for resolved, mode, reason in (
        (_vod(600.0), "full", "not a finite video of 60 s or less"),
        (_vod(30.0), "window", "not a whole video"),
    ):
        local = FakeLlmClient()
        router, _ = _router(local)
        out, clipped = await _stream(router, resolved, mode)
        assert out is not None and out[0].analysis["fallback"] == reason
        assert _no_video(local) and clipped == []


async def test_stream_native_failure_falls_back() -> None:
    local = VideoFailingClient()
    router, xai = _router(local)
    out, _ = await _stream(router, _vod(20.0))
    assert out is not None and out[0].summary == "a frames summary"
    assert out[0].analysis["fallback"] == "native call failed: LlmTransientError"
