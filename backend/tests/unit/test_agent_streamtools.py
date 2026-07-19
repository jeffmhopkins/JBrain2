"""The `analyze_stream` agent tool: resolve a video URL (faked), sample it (faked),
run the shared caption→fuse→reduce over faked models, and return the summary + the
`video_analysis` card with a stream source. Pure unit tests — no yt-dlp, no ffmpeg:
an injected resolver returns a ResolvedStream and injected samplers return canned
frames/audio, so the handler's mode routing, audio gating, and view shape are what's
under test."""

from dataclasses import replace

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.streamtools import build_stream_handlers
from jbrain.captions import CaptionTrack
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


class RaisingTranscribe:
    """A whisper client that fails (e.g. its backend host can't be resolved)."""

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        raise OSError("[Errno -3] Temporary failure in name resolution")


class RaisingRouter:
    """A router whose vision/summarize call fails (an unreachable model backend)."""

    async def complete(self, *args, **kwargs):
        raise OSError("[Errno -3] Temporary failure in name resolution")

    async def effective_spec(self, task: str):
        return ("x", "grok-4.3")


class FakeSampler:
    """Records the kwargs it was called with and returns a canned StreamSample, so a
    test asserts the handler translated each mode into the right sampler call."""

    def __init__(self, sample: StreamSample) -> None:
        self._sample = sample
        self.calls: list[dict] = []

    async def __call__(self, resolved: ResolvedStream, **kw) -> StreamSample:
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


class FakeQueue:
    """Records enqueued jobs; the deferral path only ever calls `enqueue`."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    async def enqueue(self, ctx, kind, payload, *, principal_id=None, domain_code=None) -> str:
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs.append({"kind": kind, "payload": payload})
        return job_id


class FakeMediaResults:
    """In-memory MediaResults: records created rows + attached jobs for the deferral test."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.attached: list[tuple[str, str]] = []

    async def create(self, ctx, *, session_id, run_id=None) -> str:
        result_id = f"res-{len(self.created) + 1}"
        self.created.append({"session_id": session_id})
        return result_id

    async def attach_job(self, ctx, result_id, job_id) -> None:
        self.attached.append((result_id, job_id))


def _handlers(
    blobs,
    router,
    *,
    resolver,
    window=None,
    full=None,
    transcribe=None,
    queue=None,
    media_results=None,
):
    kw = {}
    if window is not None:
        kw["window_sampler"] = window
    if full is not None:
        kw["full_sampler"] = full
    if queue is not None:
        kw["queue"] = queue
    if media_results is not None:
        kw["media_results"] = media_results
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
YOUTUBE = ResolvedStream(
    media_url="https://cdn.example.com/live.m3u8",
    title="Live Cam",
    is_live=True,
    duration_s=None,
    webpage_url="https://youtube.com/live/xyz",
    provider="youtube",
    video_id="xyz123",
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
    assert data["youtube_id"] == ""  # a non-YouTube resolver → no embed


async def test_whisper_failure_degrades_to_frames_only() -> None:
    # A whisper backend that can't be reached must NOT kill the analysis — it falls back
    # to frames-only (the documented "frames-only without whisper" posture).
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8f")], audio_wav=b"RIFFwav"))
    fake = FakeLlmClient(["a frame", "a summary"])
    handlers = _handlers(
        blobs, _router(fake), resolver=_resolver(VOD), window=window, transcribe=RaisingTranscribe()
    )
    out = await handlers["analyze_stream"]({"url": "u", "mode": "window"}, CTX)
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript"] is None  # degraded, not errored
    assert [f["t_ms"] for f in out.view.data["frames"]] == [0]


async def test_model_failure_returns_clean_error_not_raw_exception() -> None:
    # An unreachable vision/summarize model surfaces as a clean, recoverable tool
    # observation — never a raw "[Errno -3] …" leaking to the model.
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8f")]))
    handlers = _handlers(blobs, RaisingRouter(), resolver=_resolver(VOD), window=window)
    out = await handlers["analyze_stream"]({"url": "u", "mode": "single"}, CTX)
    assert isinstance(out, str)
    assert "couldn't" in out.lower() and "Errno" not in out


async def test_youtube_source_carries_embed_id() -> None:
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8f")]))
    fake = FakeLlmClient(["a pad", "On the pad."])
    handlers = _handlers(blobs, _router(fake), resolver=_resolver(YOUTUBE), window=window)

    out = await handlers["analyze_stream"]({"url": "u", "mode": "single"}, CTX)
    assert isinstance(out, ToolOutput) and out.view is not None
    # A YouTube source surfaces its video id so the card can embed the synced player.
    assert out.view.data["youtube_id"] == "xyz123"


async def test_single_mode_grabs_one_frame_without_audio() -> None:
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8only")]))
    fake = FakeLlmClient(["a launch pad", "A launch pad at dusk."])
    whisper = FakeTranscribe(_transcript())
    handlers = _handlers(
        blobs, _router(fake), resolver=_resolver(LIVE), window=window, transcribe=whisper
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "single"}, CTX)

    # single → one frame, window 0, audio suppressed even though whisper is configured;
    # seek threads through (0 with none given) — it was previously dropped, so a single
    # grab always sampled t=0 regardless of the requested moment.
    assert window.calls == [{"frames": 1, "window_s": 0.0, "seek_s": 0.0, "want_audio": False}]
    assert whisper.calls == []
    assert isinstance(out, ToolOutput) and out.view is not None
    assert [f["t_ms"] for f in out.view.data["frames"]] == [0]
    assert out.view.data["mode"] == "single"


async def test_single_mode_threads_seek_for_vod() -> None:
    # Regression: `mode=single seek=T` must sample at T, not t=0. The handler passes the
    # requested seek to the window sampler (the single grab's fast path honors it).
    blobs = FakeBlobs()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8y")]))
    handlers = _handlers(
        blobs,
        _router(FakeLlmClient(["a rack", "a summary"])),
        resolver=_resolver(VOD),
        window=window,
    )

    await handlers["analyze_stream"]({"url": "u", "mode": "single", "seek": 164}, CTX)

    assert window.calls == [{"frames": 1, "window_s": 0.0, "seek_s": 164.0, "want_audio": False}]


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
    # clamped to MAX_FRAMES; no interval given → flat total (interval_s=0)
    assert full.calls == [{"frames": 24, "interval_s": 0.0, "want_audio": False}]
    assert isinstance(out, ToolOutput) and out.startswith('Analysis of "Launch Stream":')


async def test_full_mode_passes_interval_density_through() -> None:
    # The owner's "a frame every 30s" rides to the full sampler as interval_s, so a long
    # video gets density-based coverage instead of a flat total.
    full = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8a")]))
    handlers = _handlers(
        FakeBlobs(),
        _router(FakeLlmClient(["a frame", "a summary"])),
        resolver=_resolver(VOD),
        full=full,
    )

    await handlers["analyze_stream"](
        {"url": "u", "mode": "full", "interval_s": 30, "transcribe": False}, CTX
    )

    assert full.calls == [{"frames": 16, "interval_s": 30.0, "want_audio": False}]


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


# --- captions-first transcript source (full mode) --------------------------------------

CC_VOD = replace(
    VOD,
    caption=CaptionTrack(
        url="https://cc.example.com/x.json3", ext="json3", kind="manual", lang="en"
    ),
)


def _cc_transcript() -> Transcript:
    return Transcript(
        text="Provider caption text.",
        words=(Word("Provider", 0, 300, 0.9), Word("caption", 300, 600, 0.9)),
        duration_ms=8000,
    )


async def test_full_mode_prefers_provider_captions_and_skips_whisper(monkeypatch) -> None:
    # A full-mode video with a provider caption track uses those captions (whole-video,
    # instant) instead of whisper — and skips the audio ffmpeg leg entirely.
    async def fake_fetch(track, headers, *, transport=None):
        assert track.ext == "json3"
        return _cc_transcript()

    monkeypatch.setattr("jbrain.ingest.stream_analysis.fetch_caption_transcript", fake_fetch)
    full = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8a")], audio_wav=b"RIFFwav"))
    whisper = FakeTranscribe(_transcript())
    fake = FakeLlmClient(["a frame", "A whole-video summary."])
    handlers = _handlers(
        FakeBlobs(), _router(fake), resolver=_resolver(CC_VOD), full=full, transcribe=whisper
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "full"}, CTX)

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript_source"] == "captions"
    assert out.view.data["transcript"]["text"] == "Provider caption text."
    assert whisper.calls == []  # captions won → whisper never ran
    assert full.calls[0]["want_audio"] is False  # audio leg skipped


async def test_captions_off_forces_whisper(monkeypatch) -> None:
    # `captions: off` is the re-run lever: ignore an available caption track, use whisper.
    fetched: list[int] = []

    async def fake_fetch(track, headers, *, transport=None):  # pragma: no cover - must not run
        fetched.append(1)
        return _cc_transcript()

    monkeypatch.setattr("jbrain.ingest.stream_analysis.fetch_caption_transcript", fake_fetch)
    full = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8a")], audio_wav=b"RIFFwav"))
    whisper = FakeTranscribe(_transcript())
    fake = FakeLlmClient(["a frame", "A summary."])
    handlers = _handlers(
        FakeBlobs(), _router(fake), resolver=_resolver(CC_VOD), full=full, transcribe=whisper
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "full", "captions": "off"}, CTX)

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript_source"] == "whisper"
    assert fetched == []  # the caption track was never fetched
    assert whisper.calls and full.calls[0]["want_audio"] is True


async def test_captions_only_takes_no_whisper_fallback(monkeypatch) -> None:
    # `captions: only` with no usable caption track yields no transcript — never whisper.
    async def fake_fetch(track, headers, *, transport=None):
        return None  # e.g. the fetch was refused / empty

    monkeypatch.setattr("jbrain.ingest.stream_analysis.fetch_caption_transcript", fake_fetch)
    full = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8a")], audio_wav=b"RIFFwav"))
    whisper = FakeTranscribe(_transcript())
    fake = FakeLlmClient(["a frame", "A summary."])
    handlers = _handlers(
        FakeBlobs(), _router(fake), resolver=_resolver(CC_VOD), full=full, transcribe=whisper
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "full", "captions": "only"}, CTX)

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript"] is None
    assert out.view.data["transcript_source"] == ""
    assert whisper.calls == [] and full.calls[0]["want_audio"] is False


async def test_full_mode_auto_falls_back_to_whisper_without_captions() -> None:
    # A captionless VOD in auto mode transcribes with whisper, tagged as the source.
    full = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8a")], audio_wav=b"RIFFwav"))
    whisper = FakeTranscribe(_transcript())
    fake = FakeLlmClient(["a frame", "A summary."])
    handlers = _handlers(
        FakeBlobs(), _router(fake), resolver=_resolver(VOD), full=full, transcribe=whisper
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "full"}, CTX)

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript_source"] == "whisper"
    assert whisper.calls and full.calls[0]["want_audio"] is True


# --- the in-turn ↔ defer routing (DEFERRED_TOOL_CALLS_PLAN.md P2) ----------------------


async def test_full_mode_defers_to_a_background_job() -> None:
    # With the queue + result store wired, a full (whole-video) analysis does NOT run
    # in-turn: it opens a result row, enqueues the analyze_stream_url job, and returns a
    # `deferred` result carrying the task_status card so the loop ends the turn.
    q, mr = FakeQueue(), FakeMediaResults()
    full = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8a")]))
    window = FakeSampler(StreamSample(frames=[]))
    handlers = _handlers(
        FakeBlobs(),
        _router(FakeLlmClient([])),
        resolver=_resolver(VOD),
        window=window,
        full=full,
        queue=q,
        media_results=mr,
    )

    out = await handlers["analyze_stream"](
        {"url": "https://youtube.com/watch?v=abc", "mode": "full"}, CTX
    )

    assert isinstance(out, ToolOutput)
    assert out.deferred is not None and out.deferred.session_id == SESSION
    assert out.view is not None and out.view.view == "task_status"
    assert out.view.data["result_id"] == out.deferred.result_id
    # It kicked the job (carrying the url + mode) and never sampled in-turn.
    assert len(q.jobs) == 1 and q.jobs[0]["kind"] == "analyze_stream_url"
    assert q.jobs[0]["payload"]["url"] == "https://youtube.com/watch?v=abc"
    assert q.jobs[0]["payload"]["mode"] == "full"
    assert mr.attached == [(out.deferred.result_id, out.deferred.job_id)]
    assert full.calls == [] and window.calls == []


async def test_short_window_stays_in_turn_even_with_defer_wired() -> None:
    # The threshold routes by cost: a short window is fast, so it runs in-turn and returns
    # its video_analysis card directly — no job enqueued — even though deferral is wired.
    q, mr = FakeQueue(), FakeMediaResults()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8x")]))
    fake = FakeLlmClient(["a frame", "a summary"])
    handlers = _handlers(
        FakeBlobs(),
        _router(fake),
        resolver=_resolver(VOD),
        window=window,
        queue=q,
        media_results=mr,
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "window", "window_s": 10}, CTX)

    assert isinstance(out, ToolOutput) and out.deferred is None
    assert out.view is not None and out.view.view == "video_analysis"
    assert q.jobs == [] and window.calls  # ran in-turn, nothing deferred


async def test_long_window_defers() -> None:
    # A window longer than the in-turn budget defers, like full mode.
    q, mr = FakeQueue(), FakeMediaResults()
    window = FakeSampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8x")]))
    handlers = _handlers(
        FakeBlobs(),
        _router(FakeLlmClient([])),
        resolver=_resolver(VOD),
        window=window,
        queue=q,
        media_results=mr,
    )

    out = await handlers["analyze_stream"]({"url": "u", "mode": "window", "window_s": 90}, CTX)

    assert isinstance(out, ToolOutput) and out.deferred is not None
    assert len(q.jobs) == 1 and window.calls == []
