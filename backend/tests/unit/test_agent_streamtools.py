"""The `analyze_stream` agent tool: resolve a video URL (faked), sample it (faked),
run the shared caption→fuse→reduce over faked models, and return the summary + the
`video_analysis` card with a stream source. Pure unit tests — no yt-dlp, no ffmpeg:
an injected resolver returns a ResolvedStream and injected samplers return canned
frames/audio, so the handler's mode routing, audio gating, and view shape are what's
under test."""

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.streamtools import build_stream_handlers
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.media import SampledFrame
from jbrain.stream import ResolvedStream, StreamError, StreamSample
from jbrain.transcribe import Transcript, Word

SESSION = "11111111-1111-1111-1111-111111111111"
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
        return self.data[sha256]


class FakeTranscribe:
    def __init__(self, transcript: Transcript) -> None:
        self._transcript = transcript
        self.calls: list[dict[str, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls.append({"filename": filename, "media_type": media_type})
        return self._transcript


class FakeSampler:
    """Records the kwargs it was called with and returns a canned StreamSample, so a
    test asserts the handler translated each mode into the right sampler call."""

    def __init__(self, sample: StreamSample) -> None:
        self._sample = sample
        self.calls: list[dict] = []

    def __call__(self, resolved: ResolvedStream, **kw) -> StreamSample:
        self.calls.append(kw)
        return self._sample


def _router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter(
        {"xai": fake},
        {"agent.vision": ("xai", "grok-4.3"), "video.summarize": ("xai", "grok-4.3")},
    )


def _resolver(resolved: ResolvedStream):
    def resolve(url: str, *, max_height: int = 720) -> ResolvedStream:
        return resolved

    return resolve


def _raising_resolver(exc: StreamError):
    def resolve(url: str, *, max_height: int = 720) -> ResolvedStream:
        raise exc

    return resolve


def _handlers(blobs, router, *, resolver, window=None, full=None, transcribe=None):
    kw = {}
    if window is not None:
        kw["window_sampler"] = window
    if full is not None:
        kw["full_sampler"] = full
    return build_stream_handlers(
        blobs,
        router,
        transcribe=transcribe,
        resolver=resolver,
        **kw,  # type: ignore[arg-type]
    )


def _transcript() -> Transcript:
    return Transcript(
        text="Booster is still on the mount.",
        words=(Word("Booster", 0, 400, 0.95), Word("still", 400, 700, 0.9)),
        duration_ms=10000,
    )


VOD = ResolvedStream(
    media_url="https://cdn.example.com/v.mp4",
    title="Launch Stream",
    is_live=False,
    duration_s=600.0,
    webpage_url="https://youtube.com/watch?v=abc",
)
LIVE = ResolvedStream(
    media_url="https://cdn.example.com/live.m3u8",
    title="Live Cam",
    is_live=True,
    duration_s=None,
    webpage_url="https://youtube.com/live/xyz",
)


async def test_window_round_trip_returns_summary_and_stream_view() -> None:
    blobs = FakeBlobs()
    frames = [SampledFrame(0, b"\xff\xd8f0"), SampledFrame(1000, b"\xff\xd8f1")]
    window = FakeSampler(StreamSample(frames=frames, audio_wav=b"RIFFxxxxWAVE"))
    fake = FakeLlmClient(["a rocket", "the mount", "The booster is still on the mount."])
    whisper = FakeTranscribe(_transcript())
    handlers = _handlers(
        blobs, _router(fake), resolver=_resolver(LIVE), window=window, transcribe=whisper
    )

    out = await handlers["analyze_stream"](
        {"url": "https://youtube.com/live/xyz", "mode": "window"}, CTX
    )

    # live stream → window sampler asked for audio, no seek applied by the caller.
    assert window.calls[0]["want_audio"] is True
    assert whisper.calls == [{"filename": "stream-audio.wav", "media_type": "audio/wav"}]
    assert isinstance(out, ToolOutput)
    assert out.startswith('Analysis of "Live Cam":\nThe booster is still on the mount.')
    assert out.view is not None and out.view.view == "video_analysis"
    data = out.view.data
    assert (data["source"], data["media"], data["is_live"]) == ("stream", "video", True)
    assert data["stream_url"] == "https://youtube.com/live/xyz"
    assert [f["t_ms"] for f in data["frames"]] == [0, 1000]
    assert all(f["thumb_id"] in blobs.data for f in data["frames"])
    # Each frame carries a server-inlined thumbnail data URI so the card shows the still
    # (a stream has no served-thumbnail route); it's a data: URI, not an external URL (#9).
    assert all(f["thumb_data_uri"].startswith("data:image/jpeg;base64,") for f in data["frames"])


async def test_single_mode_grabs_one_frame_without_audio() -> None:
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8only")]))
    fake = FakeLlmClient(["a launch pad", "A launch pad at dusk."])
    whisper = FakeTranscribe(_transcript())
    handlers = _handlers(
        blobs, _router(fake), resolver=_resolver(LIVE), window=window, transcribe=whisper
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "single"}, CTX)

    # single → one frame, window 0, audio suppressed even though whisper is configured.
    assert window.calls == [{"frames": 1, "window_s": 0.0, "want_audio": False}]
    assert whisper.calls == []
    assert isinstance(out, ToolOutput) and out.view is not None
    assert [f["t_ms"] for f in out.view.data["frames"]] == [0]
    assert out.view.data["mode"] == "single"


async def test_full_mode_uses_full_sampler_with_clamped_frames() -> None:
    blobs = FakeBlobs()
    full = FakeSampler(
        StreamSample(frames=[SampledFrame(0, b"\xff\xd8a"), SampledFrame(300000, b"\xff\xd8b")])
    )
    window = FakeSampler(StreamSample(frames=[]))
    fake = FakeLlmClient(["frame a", "frame b", "A whole-video summary."])
    handlers = _handlers(blobs, _router(fake), resolver=_resolver(VOD), window=window, full=full)

    out = await handlers["analyze_stream"](
        {"url": "u", "mode": "full", "frames": 999, "transcribe": False}, CTX
    )

    assert window.calls == []  # full mode does not touch the window sampler
    assert full.calls == [{"frames": 24, "want_audio": False}]  # clamped to MAX_FRAMES
    assert isinstance(out, ToolOutput) and out.startswith('Analysis of "Launch Stream":')


async def test_window_seek_and_frames_passed_through_for_vod() -> None:
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8x")]))
    fake = FakeLlmClient(["x", "a summary"])
    handlers = _handlers(blobs, _router(fake), resolver=_resolver(VOD), window=window)

    await handlers["analyze_stream"](
        {
            "url": "u",
            "mode": "window",
            "frames": 3,
            "window_s": 20,
            "seek": 42,
            "transcribe": False,
        },
        CTX,
    )
    assert window.calls == [{"frames": 3, "window_s": 20.0, "seek_s": 42.0, "want_audio": False}]


async def test_empty_url_and_bad_mode_are_recoverable_messages() -> None:
    handlers = _handlers(FakeBlobs(), _router(FakeLlmClient([])), resolver=_resolver(VOD))
    assert await handlers["analyze_stream"]({"url": ""}, CTX) == "analyze_stream needs a url."
    msg = await handlers["analyze_stream"]({"url": "u", "mode": "clip"}, CTX)
    assert "single, window, full" in msg


async def test_resolve_error_is_surfaced() -> None:
    handlers = _handlers(
        FakeBlobs(), _router(FakeLlmClient([])), resolver=_raising_resolver(StreamError("nope"))
    )
    assert await handlers["analyze_stream"]({"url": "u"}, CTX) == "nope"


async def test_empty_sample_reports_no_frames() -> None:
    window = FakeSampler(StreamSample(frames=[], audio_wav=b""))
    handlers = _handlers(
        FakeBlobs(), _router(FakeLlmClient([])), resolver=_resolver(VOD), window=window
    )
    out = await handlers["analyze_stream"]({"url": "u", "transcribe": False}, CTX)
    assert isinstance(out, str) and "couldn't read any frames" in out


async def test_audio_skipped_when_whisper_unconfigured() -> None:
    blobs = FakeBlobs()
    window = FakeSampler(
        StreamSample(frames=[SampledFrame(0, b"\xff\xd8f")], audio_wav=b"RIFFwave")
    )
    fake = FakeLlmClient(["a frame", "a summary"])
    # No transcribe client → want_audio is False regardless of the audio bytes present.
    handlers = _handlers(blobs, _router(fake), resolver=_resolver(LIVE), window=window)
    out = await handlers["analyze_stream"]({"url": "u", "mode": "window"}, CTX)
    assert window.calls[0]["want_audio"] is False
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript"] is None
