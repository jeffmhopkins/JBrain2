"""The `analyze_video` agent tool: resolve a chat video attachment by id, run the
inline map→fuse→reduce over faked services, and return the summary + the
`video_analysis` card.

Pure unit tests — in-memory attachment repo / blob store, faked vision LLM, faked
whisper, a canned frame sampler (no ffmpeg). RLS is modeled by membership (an
unknown id reads as missing)."""

from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.videotools import build_video_handlers
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.media import SampledFrame
from jbrain.transcribe import Transcript, Word

SESSION = "11111111-1111-1111-1111-111111111111"
ATT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CTX = ToolContext(
    session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=SESSION
)


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


class FakeAttachments:
    """session_read_context returns a context only for the bound session; get is
    membership-scoped (an unknown id reads as missing, modeling RLS)."""

    def __init__(self) -> None:
        self.rows: dict[str, AttachmentInfo] = {}
        self.cache: dict[str, dict] = {}  # the persisted analyze_video result, by id

    def add(
        self, attachment_id: str, *, media_type: str, sha: str, filename: str, size_bytes: int = 10
    ) -> None:
        self.rows[attachment_id] = AttachmentInfo(
            id=attachment_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha,
            domain_code="general",
        )

    async def session_read_context(
        self, ctx: SessionContext, agent_session_id: str
    ) -> SessionContext | None:
        return ctx if agent_session_id == SESSION else None

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        return self.rows.get(attachment_id)

    async def analysis(self, ctx: SessionContext, attachment_id: str) -> dict | None:
        return self.cache.get(attachment_id)

    async def set_analysis(self, ctx: SessionContext, attachment_id: str, analysis: dict) -> None:
        self.cache[attachment_id] = analysis


class FakeTranscribe:
    def __init__(self, transcript: Transcript | Exception) -> None:
        self._transcript = transcript
        self.calls: list[dict[str, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls.append({"filename": filename, "media_type": media_type})
        if isinstance(self._transcript, Exception):
            raise self._transcript
        return self._transcript


def _router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter(
        {"xai": fake},
        {"agent.vision": ("xai", "grok-4.3"), "video.summarize": ("xai", "grok-4.3")},
    )


def _sampler(frames: list[SampledFrame]):
    def _sample(video: bytes) -> list[SampledFrame]:
        return list(frames)

    return _sample


def _setup(
    *, media_type: str = "video/mp4", size_bytes: int = 10
) -> tuple[FakeBlobs, FakeAttachments]:
    blobs = FakeBlobs()
    attachments = FakeAttachments()
    attachments.add(
        ATT, media_type=media_type, sha="vid-sha", filename="clip.mp4", size_bytes=size_bytes
    )
    blobs.data["vid-sha"] = b"fake video bytes"
    return blobs, attachments


def _transcript() -> Transcript:
    return Transcript(
        text="First we ingest. Then a worker runs.",
        words=(
            Word("First", 1000, 1300, 0.95),
            Word("we", 1300, 1450, 0.9),
            Word("ingest.", 1450, 1900, 0.8),
            Word("Then", 5000, 5300, 0.94),
            Word("a", 5300, 5400, 0.96),
            Word("worker", 5400, 5800, 0.85),
            Word("runs.", 5800, 6200, 0.9),
        ),
        duration_ms=8000,
    )


async def test_round_trip_returns_summary_and_video_view() -> None:
    blobs, attachments = _setup()
    frames = [SampledFrame(0, b"\xff\xd8frame0"), SampledFrame(4000, b"\xff\xd8frame1")]
    fake = FakeLlmClient(
        ["A title card.", "A pipeline diagram.", "A walkthrough of a build pipeline."]
    )
    whisper = FakeTranscribe(_transcript())
    handlers = build_video_handlers(
        blobs, attachments, _router(fake), transcribe=whisper, sampler=_sampler(frames)
    )

    out = await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)

    # One vision call per frame (each carrying that JPEG), then one text-only summary.
    assert [len(c["images"]) for c in fake.calls] == [1, 1, 0]
    assert fake.calls[0]["images"][0].media_type == "image/jpeg"
    assert whisper.calls == [{"filename": "clip.mp4", "media_type": "video/mp4"}]
    assert isinstance(out, ToolOutput)
    assert out.startswith('Analysis of "clip.mp4":\nA walkthrough')
    assert out.view is not None and out.view.view == "video_analysis"
    data = out.view.data
    assert (data["attachment_id"], data["source"], data["media"]) == (ATT, "chat", "video")
    assert data["summary"].startswith("A walkthrough")
    assert [f["t_ms"] for f in data["frames"]] == [0, 4000]
    assert all(f["thumb_id"] in blobs.data for f in data["frames"])  # thumbs were stored
    assert data["transcript"]["text"].startswith("First we ingest")
    assert data["duration_ms"] == 8000
    # The result was cached on the attachment so a re-ask is free + thumbs are servable.
    assert attachments.cache[ATT]["summary"].startswith("A walkthrough")


async def test_reanalysis_reads_the_cache_without_re_billing() -> None:
    blobs, attachments = _setup()
    frames = [SampledFrame(0, b"\xff\xd8f0"), SampledFrame(4000, b"\xff\xd8f1")]
    first = FakeLlmClient(["frame a", "frame b", "a summary"])
    handlers = build_video_handlers(blobs, attachments, _router(first), sampler=_sampler(frames))
    await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert len(first.calls) == 3

    # A second ask finds the cached analysis and bills nothing — same card.
    second = FakeLlmClient(["should-not-run"])
    second_whisper = FakeTranscribe(_transcript())
    cached_handlers = build_video_handlers(
        blobs, attachments, _router(second), transcribe=second_whisper, sampler=_sampler(frames)
    )
    out = await cached_handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert second.calls == []
    assert second_whisper.calls == []
    assert isinstance(out, ToolOutput) and out.view is not None
    assert [f["t_ms"] for f in out.view.data["frames"]] == [0, 4000]


async def test_frames_only_when_no_whisper() -> None:
    blobs, attachments = _setup()
    fake = FakeLlmClient(["A single frame.", "A frames-only summary."])
    handlers = build_video_handlers(
        blobs, attachments, _router(fake), sampler=_sampler([SampledFrame(0, b"\xff\xd8f")])
    )

    out = await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript"] is None
    assert len(out.view.data["frames"]) == 1


async def test_rejects_non_video_attachment() -> None:
    blobs, attachments = _setup(media_type="image/png")
    fake = FakeLlmClient(["unused"])
    handlers = build_video_handlers(blobs, attachments, _router(fake), sampler=_sampler([]))
    out = await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert "isn't a video" in out
    assert fake.calls == []  # rejected before any spend


async def test_unknown_and_foreign_ids_read_as_a_clean_miss() -> None:
    blobs, attachments = _setup()
    fake = FakeLlmClient(["unused"])
    handlers = build_video_handlers(blobs, attachments, _router(fake), sampler=_sampler([]))
    # Unknown id in this session.
    assert "No attached video" in await handlers["analyze_video"](
        {"source_attachment_id": "22222222-2222-2222-2222-222222222222"}, CTX
    )
    # A non-uuid / empty id.
    assert "No attached video" in await handlers["analyze_video"]({"source_attachment_id": ""}, CTX)
    # A different session can't reach the file (session_read_context returns None).
    other = ToolContext(
        session=CTX.session, scopes=(), agent_session_id="deadbeef-0000-0000-0000-000000000000"
    )
    assert "No attached video" in await handlers["analyze_video"](
        {"source_attachment_id": ATT}, other
    )
    assert fake.calls == []


async def test_oversize_video_is_refused_before_any_spend() -> None:
    blobs, attachments = _setup(size_bytes=10)
    fake = FakeLlmClient(["unused"])
    handlers = build_video_handlers(
        blobs, attachments, _router(fake), sampler=_sampler([SampledFrame(0, b"x")]), max_bytes=5
    )
    out = await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert "too large" in out
    assert fake.calls == []


async def test_empty_clip_reports_nothing_found() -> None:
    blobs, attachments = _setup()
    fake = FakeLlmClient(["unused"])
    whisper = FakeTranscribe(Transcript(text="   "))
    handlers = build_video_handlers(
        blobs, attachments, _router(fake), transcribe=whisper, sampler=_sampler([])
    )
    out = await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert "couldn't read any frames or speech" in out
    assert fake.calls == []  # no frames to caption, no timeline to summarize


async def test_model_failure_is_a_recoverable_observation() -> None:
    blobs, attachments = _setup()
    fake = FakeLlmClient(["a frame"])
    whisper = FakeTranscribe(RuntimeError("gateway down"))
    handlers = build_video_handlers(
        blobs,
        attachments,
        _router(fake),
        transcribe=whisper,
        sampler=_sampler([SampledFrame(0, b"\xff\xd8f")]),
    )
    out = await handlers["analyze_video"]({"source_attachment_id": ATT}, CTX)
    assert "couldn't analyze that video right now" in out
    assert not isinstance(out, ToolOutput)  # plain error string, no card
