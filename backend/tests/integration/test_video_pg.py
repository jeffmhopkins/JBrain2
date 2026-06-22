"""Migration 0083 + the analyze_video attachment chain against real Postgres:
video_analysis-extract RLS isolation (CLAUDE.md rule 3 — the same firewall the
ocr/transcript extracts get, asserted for the new kind) and the
analyze_video_attachment round trip (blob -> sampled+captioned frames + faked
transcript -> fused summary extract row + per-frame thumbnails as blobs ->
re-ingest builds a searchable video_analysis chunk), with the LLM, whisper, and
frame sampler all faked. Also: the cache suppresses re-analysis, whisper-off
degrades to a frames-only analysis, gone rows no-op, and an empty clip caches
nothing."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.ingest.video import (
    FRAME_SYSTEM,
    KIND_VIDEO_ANALYSIS,
    SUMMARY_SYSTEM,
    VIDEO_ANALYSIS_CONFIDENCE,
    VideoPipeline,
)
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.media import SampledFrame
from jbrain.models.notes import AttachmentExtract
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from jbrain.transcribe import Transcript, Word
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


class FakeTranscribeClient:
    """Scripted whisper client: records every call, returns the next transcript."""

    def __init__(self, transcripts: list[Transcript]):
        self._transcripts = list(transcripts)
        self.calls: list[dict[str, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls.append({"filename": filename, "media_type": media_type})
        return self._transcripts.pop(0)


class FakeGateway:
    """Records unload() calls so the unload-after behavior is observable."""

    def __init__(self) -> None:
        self.unloaded: list[str] = []

    async def running(self) -> set[str]:
        return set()

    async def load(self, served_model: str) -> None:
        return None

    async def unload(self, served_model: str) -> None:
        self.unloaded.append(served_model)


def fake_sampler(frames: list[SampledFrame]):
    """A sampler that ignores the bytes and returns canned frames (no ffmpeg)."""

    def _sample(video: bytes) -> list[SampledFrame]:
        return list(frames)

    return _sample


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


async def make_note_with_video(
    maker: async_sessionmaker[AsyncSession],
    blobs: FsBlobStore | None = None,
    *,
    domain: str = "general",
    body: str = "screen recording of the build",
    filename: str = "walkthrough.mp4",
    data: bytes = b"\x00\x00\x00\x18ftypmp42 fake video bytes",
) -> tuple[str, str]:
    """A note + one video attachment; returns (note_id, attachment_id)."""
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"vid-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    digest = await blobs.put(data) if blobs is not None else "00" * 32
    attachment = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256=digest,
        filename=filename,
        media_type="video/mp4",
        size_bytes=len(data),
    )
    assert attachment is not None
    return note.id, attachment.id


def video_router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter(
        {"xai": fake},
        {"agent.vision": ("xai", "grok-4.3"), "video.summarize": ("xai", "grok-4.3")},
    )


def jpeg(marker: bytes) -> bytes:
    return b"\xff\xd8\xff" + marker  # a plausible JPEG header + unique tail


async def analysis_visible(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, extract_id: str
) -> int:
    async with scoped_session(maker, ctx) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.attachment_extracts WHERE id = :id"),
                {"id": extract_id},
            )
        ).scalar_one()


async def video_extract(
    maker: async_sessionmaker[AsyncSession], attachment_id: str
) -> AttachmentExtract | None:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                select(AttachmentExtract).where(
                    AttachmentExtract.attachment_id == attachment_id,
                    AttachmentExtract.kind == KIND_VIDEO_ANALYSIS,
                )
            )
        ).scalar_one_or_none()


async def ingest_jobs_for(maker: async_sessionmaker[AsyncSession], note_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ingest_note'"
                    " AND payload->>'note_id' = :nid"
                ),
                {"nid": note_id},
            )
        ).scalar_one()


# --- RLS isolation ----------------------------------------------------------


async def test_video_analysis_extract_enforces_domain_firewall(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Rule 3: a health-domain video analysis is invisible without health scope."""
    _, attachment_id = await make_note_with_video(maker, domain="health")
    extract_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.attachment_extracts"
                " (id, attachment_id, kind, tool, text, confidence, analysis,"
                "  source_anchor, domain_code)"
                " VALUES (:id, :aid, 'video_analysis', 'xai:grok-4.3', 'a clinic visit clip',"
                " 0.6, '{\"frames\": []}'::jsonb, 'walkthrough.mp4', 'health')"
            ),
            {"id": extract_id, "aid": attachment_id},
        )

    assert await analysis_visible(maker, HEALTH_ONLY, extract_id) == 1
    assert await analysis_visible(maker, OWNER, extract_id) == 1
    assert await analysis_visible(maker, GENERAL_ONLY, extract_id) == 0
    assert await analysis_visible(maker, UNSCOPED, extract_id) == 0


# --- the analyze_video_attachment round trip --------------------------------


def transcript_fixture() -> Transcript:
    return Transcript(
        text="First we ingest the note. Then a worker processes it.",
        language="en",
        words=(
            Word("First", 1000, 1300, 0.95),
            Word("we", 1300, 1450, 0.97),
            Word("ingest", 1450, 1800, 0.8),
            Word("the", 1800, 1950, 0.97),
            Word("note.", 1950, 2400, 0.9),
            Word("Then", 5000, 5300, 0.94),
            Word("a", 5300, 5400, 0.96),
            Word("worker", 5400, 5800, 0.85),
            Word("processes", 5800, 6300, 0.8),
            Word("it.", 6300, 6600, 0.9),
        ),
        duration_ms=8000,
    )


async def test_video_round_trip_blob_to_searchable_summary(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_video(maker, blobs)
    frames = [
        SampledFrame(timestamp_ms=0, jpeg=jpeg(b"frame0")),
        SampledFrame(timestamp_ms=4000, jpeg=jpeg(b"frame1")),
    ]
    fake = FakeLlmClient(
        [
            "A title card reading 'Build pipeline'.",
            "A diagram of INGEST to QUEUE to WORKER.",
            "A whiteboard walkthrough of a build pipeline: the note is ingested, queued,"
            " and processed by a worker.",
        ]
    )
    whisper = FakeTranscribeClient([transcript_fixture()])
    gateway = FakeGateway()
    pipeline = VideoPipeline(
        maker,
        blobs,
        video_router(fake),
        transcribe=whisper,
        transcribe_model="whisper-large-v3",
        gateway=gateway,
        sampler=fake_sampler(frames),
    )

    await pipeline.analyze_video_attachment({"attachment_id": attachment_id})

    # Map: one vision call per frame (each carrying that JPEG), then one text-only
    # reduce call over the fused timeline. Whisper ran once and was unloaded after.
    assert [c["system"] for c in fake.calls] == [FRAME_SYSTEM, FRAME_SYSTEM, SUMMARY_SYSTEM]
    assert [len(c["images"]) for c in fake.calls] == [1, 1, 0]
    assert fake.calls[0]["images"][0].media_type == "image/jpeg"
    assert whisper.calls == [{"filename": "walkthrough.mp4", "media_type": "video/mp4"}]
    assert gateway.unloaded == ["whisper-large-v3"]
    # The reduce step saw the fused [mm:ss] timeline — frames and speech, in order.
    timeline = fake.calls[2]["user_text"]
    assert "[00:00] (frame) A title card" in timeline
    assert "[00:01] (said) “First we ingest the note.”" in timeline
    assert "[00:04] (frame) A diagram" in timeline

    row = await video_extract(maker, attachment_id)
    assert row is not None
    assert row.tool == "xai:grok-4.3"
    assert row.source_anchor == "walkthrough.mp4"
    assert row.text.startswith("A whiteboard walkthrough")
    assert row.confidence == pytest.approx(VIDEO_ANALYSIS_CONFIDENCE)  # the Guards cap
    # The structured analysis: per-frame timeline (thumb ids) + the fused transcript.
    analysis = row.analysis
    assert analysis is not None
    assert analysis["duration_ms"] == 8000
    assert [f["t_ms"] for f in analysis["frames"]] == [0, 4000]
    assert [f["caption"] for f in analysis["frames"]][0].startswith("A title card")
    assert analysis["transcript"]["text"].startswith("First we ingest")
    assert len(analysis["transcript"]["words"]) == 10

    # Each kept frame's JPEG is a content-addressed blob the timeline points at by id.
    for frame, thumb in zip(frames, analysis["frames"], strict=True):
        assert await blobs.get(thumb["thumb_id"]) == frame.jpeg

    # The handler re-enqueued ingest; running it makes the summary a searchable chunk.
    assert await ingest_jobs_for(maker, note_id) == 1
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    async with scoped_session(maker, OWNER) as s:
        kinds = set(
            (
                await s.execute(
                    text(
                        "SELECT source_kind FROM app.chunks"
                        " WHERE note_id = :nid AND attachment_id = :aid"
                    ),
                    {"nid": note_id, "aid": attachment_id},
                )
            ).scalars()
        )
        hits = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.chunks WHERE note_id = :nid"
                    " AND tsv @@ plainto_tsquery('english', 'build pipeline worker')"
                ),
                {"nid": note_id},
            )
        ).scalar_one()
    assert "video_analysis" in kinds
    assert hits >= 1


async def test_video_reanalysis_skips_when_cached(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_video(maker, blobs)
    frames = [SampledFrame(timestamp_ms=0, jpeg=jpeg(b"f0"))]

    def run(fake: FakeLlmClient, whisper: FakeTranscribeClient) -> VideoPipeline:
        return VideoPipeline(
            maker,
            blobs,
            video_router(fake),
            transcribe=whisper,
            transcribe_model="whisper-large-v3",
            sampler=fake_sampler(frames),
        )

    first = FakeLlmClient(["a frame", "a summary"])
    first_whisper = FakeTranscribeClient([transcript_fixture()])
    await run(first, first_whisper).analyze_video_attachment({"attachment_id": attachment_id})
    assert len(first.calls) == 2

    # A second run finds the cache row and bills neither the vision nor whisper model.
    second = FakeLlmClient(["should-not-run"])
    second_whisper = FakeTranscribeClient([transcript_fixture()])
    await run(second, second_whisper).analyze_video_attachment({"attachment_id": attachment_id})
    assert second.calls == []
    assert second_whisper.calls == []


async def test_video_frames_only_when_whisper_unconfigured(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """whisper off -> no transcribe client: a frames-only analysis, transcript NULL."""
    _, attachment_id = await make_note_with_video(maker, blobs)
    frames = [SampledFrame(timestamp_ms=0, jpeg=jpeg(b"f0"))]
    fake = FakeLlmClient(["a single frame caption", "a frames-only summary"])
    pipeline = VideoPipeline(
        maker, blobs, video_router(fake), transcribe=None, sampler=fake_sampler(frames)
    )

    await pipeline.analyze_video_attachment({"attachment_id": attachment_id})

    assert [c["system"] for c in fake.calls] == [FRAME_SYSTEM, SUMMARY_SYSTEM]
    row = await video_extract(maker, attachment_id)
    assert row is not None
    analysis = row.analysis
    assert analysis is not None
    assert analysis["transcript"] is None
    assert len(analysis["frames"]) == 1


async def test_video_handler_noops_when_attachment_or_note_is_gone(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    fake = FakeLlmClient(["nope"])
    whisper = FakeTranscribeClient([transcript_fixture()])
    pipeline = VideoPipeline(
        maker,
        blobs,
        video_router(fake),
        transcribe=whisper,
        sampler=fake_sampler([SampledFrame(0, jpeg(b"f0"))]),
    )
    await pipeline.analyze_video_attachment({"attachment_id": str(uuid.uuid4())})

    note_id, attachment_id = await make_note_with_video(maker, blobs)
    assert await SqlNotesRepo(maker).delete_note(OWNER, note_id)
    await pipeline.analyze_video_attachment({"attachment_id": attachment_id})

    assert fake.calls == []  # neither skip path may bill a model
    assert whisper.calls == []
    assert await video_extract(maker, attachment_id) is None


async def test_video_empty_clip_caches_nothing(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """No decodable frames and no speech: write no marker so the on-demand tool
    re-tries rather than caching a dead empty analysis."""
    note_id, attachment_id = await make_note_with_video(maker, blobs)
    fake = FakeLlmClient(["unused"])
    silent = FakeTranscribeClient([Transcript(text="   ")])
    pipeline = VideoPipeline(
        maker, blobs, video_router(fake), transcribe=silent, sampler=fake_sampler([])
    )

    await pipeline.analyze_video_attachment({"attachment_id": attachment_id})

    assert fake.calls == []  # no frames to caption, no timeline to summarize
    assert await video_extract(maker, attachment_id) is None
    assert await ingest_jobs_for(maker, note_id) == 0
