"""The deferred analyze_stream worker job (StreamAnalysisPipeline) against real Postgres
(DEFERRED_TOOL_CALLS_PLAN.md P2): the happy path writes progress + the finished card data
onto the result row, and a Stop mid-flight is honored promptly by the internal cancel
watcher — the analysis is cancelled and the row stays 'canceled'. yt-dlp, ffmpeg, whisper,
and the LLM are all faked; only the result-row storage + the cancel race are real."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent import media_results
from jbrain.ingest import stream_analysis
from jbrain.ingest.stream_analysis import StreamAnalysisPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.media import SampledFrame
from jbrain.storage import FsBlobStore
from jbrain.stream import ResolvedStream, StreamSample
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

VOD = ResolvedStream(
    media_url="https://cdn.example.com/v.mp4",
    title="Launch Stream",
    is_live=False,
    duration_s=600.0,
    webpage_url="https://youtube.com/watch?v=abc",
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter(
        {"xai": fake},
        {"agent.vision": ("xai", "grok-4.3"), "video.summarize": ("xai", "grok-4.3")},
    )


def _resolver(resolved: ResolvedStream):
    def resolve(url: str, *, max_height: int = 720) -> ResolvedStream:
        return resolved

    return resolve


def _sampler(sample: StreamSample):
    async def sample_fn(resolved: ResolvedStream, **kw) -> StreamSample:
        return sample

    return sample_fn


async def test_deferred_job_writes_the_finished_card_to_the_result_row(
    maker: async_sessionmaker, tmp_path: Path
) -> None:
    blobs = FsBlobStore(tmp_path)
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    full = _sampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8\xff frame")]))
    fake = FakeLlmClient(["a rocket on a pad", "A launch stream showing the rocket."])
    pipeline = StreamAnalysisPipeline(
        maker, blobs, _router(fake), resolver=_resolver(VOD), full_sampler=full
    )

    await pipeline.analyze_stream_url({"result_id": rid, "url": "u", "mode": "full"})

    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "done" and row.result is not None
    # The stored result is the video_analysis card data — the status card swaps to it.
    assert row.result["source"] == "stream"
    assert row.result["mode"] == "full"
    assert row.result["summary"] == "A launch stream showing the rocket."
    assert len(row.result["frames"]) == 1
    # The auto-resume report jerv is prompted with carries the summary (P3).
    assert "A launch stream showing the rocket." in str(row.result["resume_message"])


async def test_deferred_job_uses_provider_captions_when_available(
    maker: async_sessionmaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caption-bearing video defers, and the worker transcribes from the provider's
    # captions (no whisper client wired) — the finished card tags its source.
    from jbrain.captions import CaptionTrack
    from jbrain.transcribe import Transcript, Word

    async def fake_fetch(track, headers, *, transport=None):
        return Transcript(text="Captioned words.", words=(Word("Captioned", 0, 300, 0.9),))

    monkeypatch.setattr(stream_analysis, "fetch_caption_transcript", fake_fetch)
    cc_vod = ResolvedStream(
        media_url="https://cdn.example.com/v.mp4",
        title="Captioned Talk",
        is_live=False,
        duration_s=600.0,
        webpage_url="https://youtube.com/watch?v=cc",
        caption=CaptionTrack(
            url="https://cc.example.com/x.json3", ext="json3", kind="auto", lang="en"
        ),
    )
    blobs = FsBlobStore(tmp_path)
    rid = await media_results.create(maker, OWNER, session_id="chat-1")
    full = _sampler(StreamSample(frames=[SampledFrame(0, b"\xff\xd8\xff frame")]))
    fake = FakeLlmClient(["a slide", "A talk about the thing."])
    pipeline = StreamAnalysisPipeline(
        maker, blobs, _router(fake), resolver=_resolver(cc_vod), full_sampler=full
    )

    await pipeline.analyze_stream_url({"result_id": rid, "url": "u", "mode": "full"})

    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "done" and row.result is not None
    assert row.result["transcript_source"] == "captions"
    assert row.result["transcript"]["text"] == "Captioned words."
    # The auto-resume report carries the caption transcript so jerv can quote it.
    assert "Captioned words." in str(row.result["resume_message"])


async def test_stop_mid_flight_cancels_the_analysis(
    maker: async_sessionmaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sampler that blocks forever stands in for a long ffmpeg/whisper leg; the watcher
    # must notice the Stop (row → 'canceled') and cancel the analysis, which unblocks it.
    monkeypatch.setattr(stream_analysis, "_CANCEL_POLL_S", 0.05)
    blobs = FsBlobStore(tmp_path)
    rid = await media_results.create(maker, OWNER, session_id="chat-1")

    entered = asyncio.Event()

    async def blocking_sampler(resolved: ResolvedStream, **kw) -> StreamSample:
        entered.set()
        await asyncio.Event().wait()  # never resolves — a stand-in for a long leg
        raise AssertionError("unreachable")

    pipeline = StreamAnalysisPipeline(
        maker,
        blobs,
        _router(FakeLlmClient([])),
        resolver=_resolver(VOD),
        full_sampler=blocking_sampler,
    )
    job = asyncio.ensure_future(
        pipeline.analyze_stream_url({"result_id": rid, "url": "u", "mode": "full"})
    )

    await asyncio.wait_for(entered.wait(), timeout=5)  # the analysis is now blocked
    assert await media_results.cancel(maker, OWNER, rid) is True  # the owner taps Stop

    # The watcher cancels the analysis; the job returns cleanly (never re-raises) and the
    # row stays canceled — no late completion resurrects it.
    await asyncio.wait_for(job, timeout=5)
    row = await media_results.get(maker, OWNER, rid)
    assert row is not None and row.status == "canceled" and row.result is None
