"""The audio-transcription attachment chain against real Postgres: the
transcript extract's RLS isolation (CLAUDE.md rule 3 — the same firewall the OCR
extracts get, asserted for the new kind) and the transcribe_attachment round trip
(blob -> faked transcript -> extract row -> re-ingest builds a transcript chunk ->
FTS finds it), with the whisper client always faked. Also: ingest only enqueues
when the backend is configured, the oversize budget, and the unload-after call."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.ingest.transcribe_job import TRANSCRIPT_CONFIDENCE, TranscribePipeline
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


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


def ingest(maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore, *, enabled: bool = True):
    return IngestPipeline(maker, blobs, transcribe_enabled=enabled)


async def make_note_with_audio(
    maker: async_sessionmaker[AsyncSession],
    blobs: FsBlobStore | None = None,
    *,
    domain: str = "general",
    body: str = "voice memo from the meeting",
    filename: str = "memo.wav",
    data: bytes = b"RIFF fake audio bytes",
    size_bytes: int | None = None,
) -> tuple[str, str]:
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"tr-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    digest = await blobs.put(data) if blobs is not None else "00" * 32
    attachment = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256=digest,
        filename=filename,
        media_type="audio/wav",
        size_bytes=size_bytes if size_bytes is not None else len(data),
    )
    assert attachment is not None
    return note.id, attachment.id


async def transcribe_jobs_for(maker: async_sessionmaker[AsyncSession], attachment_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'transcribe_attachment'"
                    " AND payload->>'attachment_id' = :aid"
                ),
                {"aid": attachment_id},
            )
        ).scalar_one()


async def transcript_visible(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, extract_id: str
) -> int:
    async with scoped_session(maker, ctx) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.attachment_extracts WHERE id = :id"),
                {"id": extract_id},
            )
        ).scalar_one()


async def test_transcript_extract_enforces_domain_firewall(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Rule 3: a health-domain transcript is invisible without health scope."""
    _, attachment_id = await make_note_with_audio(maker, domain="health")
    extract_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.attachment_extracts"
                " (id, attachment_id, kind, tool, text, confidence, source_anchor, domain_code)"
                " VALUES (:id, :aid, 'transcript', 'whisper:w', 'my blood pressure was high',"
                " 0.8, 'memo.wav', 'health')"
            ),
            {"id": extract_id, "aid": attachment_id},
        )

    assert await transcript_visible(maker, HEALTH_ONLY, extract_id) == 1
    assert await transcript_visible(maker, OWNER, extract_id) == 1
    assert await transcript_visible(maker, GENERAL_ONLY, extract_id) == 0
    assert await transcript_visible(maker, UNSCOPED, extract_id) == 0


async def test_transcribe_round_trip_blob_to_searchable_chunk(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_audio(maker, blobs)

    # Ingest is model-free: it only enqueues the transcription job for the
    # uncached audio, and analysis is deferred (outstanding transcribe work).
    await ingest(maker, blobs).ingest_note({"note_id": note_id})
    assert await transcribe_jobs_for(maker, attachment_id) == 1
    # Enqueue-once: a re-ingest while the job is still queued adds nothing.
    await ingest(maker, blobs).ingest_note({"note_id": note_id})
    assert await transcribe_jobs_for(maker, attachment_id) == 1

    gateway = FakeGateway()
    fake = FakeTranscribeClient(
        [
            Transcript(
                text="Discussed the Q3 roadmap.",
                language="en",
                words=(
                    Word("Discussed", 0, 600, 0.95),
                    Word("the", 600, 800, 0.97),
                    Word("Q3", 800, 1100, 0.61),
                    Word("roadmap.", 1100, 1700, 0.9),
                ),
                duration_ms=1700,
            )
        ]
    )
    handler = TranscribePipeline(maker, blobs, fake, "whisper-large-v3", gateway=gateway)
    await handler.transcribe_attachment({"attachment_id": attachment_id})

    # One transcription call carrying the audio, then the model is unloaded.
    assert fake.calls == [{"filename": "memo.wav", "media_type": "audio/wav"}]
    assert gateway.unloaded == ["whisper-large-v3"]

    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT kind, tool, text, confidence, source_anchor"
                    " FROM app.attachment_extracts WHERE attachment_id = :aid"
                ),
                {"aid": attachment_id},
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]._mapping
    assert (row["kind"], row["tool"], row["source_anchor"]) == (
        "transcript",
        "whisper:whisper-large-v3",
        "memo.wav",
    )
    # The per-word breakdown is stored as JSONB for the karaoke UI.
    async with scoped_session(maker, OWNER) as s:
        words = (
            await s.execute(
                text(
                    "SELECT words FROM app.attachment_extracts"
                    " WHERE attachment_id = :aid AND kind = 'transcript'"
                ),
                {"aid": attachment_id},
            )
        ).scalar_one()
    assert [w["text"] for w in words] == ["Discussed", "the", "Q3", "roadmap."]
    assert words[2] == {"text": "Q3", "start_ms": 800, "end_ms": 1100, "confidence": 0.61}
    assert row["confidence"] == pytest.approx(TRANSCRIPT_CONFIDENCE)

    # The handler re-enqueued ingest; run it and the transcript becomes a chunk.
    await ingest(maker, blobs).ingest_note({"note_id": note_id})
    async with scoped_session(maker, OWNER) as s:
        chunks = (
            await s.execute(
                text(
                    "SELECT source_kind, source_anchor, text FROM app.chunks"
                    " WHERE note_id = :nid AND attachment_id = :aid ORDER BY seq"
                ),
                {"nid": note_id, "aid": attachment_id},
            )
        ).all()
        hits = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.chunks WHERE note_id = :nid"
                    " AND tsv @@ plainto_tsquery('english', 'roadmap')"
                ),
                {"nid": note_id},
            )
        ).scalar_one()
    assert {(c.source_kind, c.source_anchor) for c in chunks} == {("transcript", "memo.wav")}
    assert any("Q3 roadmap" in c.text for c in chunks)
    assert hits >= 1  # FTS finds the transcript immediately

    # The cache suppresses re-transcription: this re-ingest enqueues no new job.
    assert await transcribe_jobs_for(maker, attachment_id) == 1


async def test_ingest_skips_transcription_when_backend_unconfigured(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """Empty whisper_url (transcribe_enabled False): no job, no chunks — graceful."""
    note_id, attachment_id = await make_note_with_audio(maker, blobs)
    await ingest(maker, blobs, enabled=False).ingest_note({"note_id": note_id})
    assert await transcribe_jobs_for(maker, attachment_id) == 0


async def test_ingest_skips_transcription_for_oversized_audio(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_audio(maker, blobs, filename="huge.wav")
    pipeline = IngestPipeline(maker, blobs, transcribe_enabled=True, transcribe_max_bytes=8)
    await pipeline.ingest_note({"note_id": note_id})
    assert await transcribe_jobs_for(maker, attachment_id) == 0


async def test_transcribe_handler_noops_when_attachment_is_gone(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    fake = FakeTranscribeClient([])
    gateway = FakeGateway()
    handler = TranscribePipeline(maker, blobs, fake, "w", gateway=gateway)
    await handler.transcribe_attachment({"attachment_id": str(uuid.uuid4())})
    assert fake.calls == []  # never reached the model
    assert gateway.unloaded == []


async def test_empty_transcript_caches_a_zero_confidence_marker(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """Silence/non-speech audio still caches a row (confidence 0) so re-ingest
    does not loop back into transcription."""
    note_id, attachment_id = await make_note_with_audio(maker, blobs)
    fake = FakeTranscribeClient([Transcript(text="   ")])
    await TranscribePipeline(maker, blobs, fake, "w").transcribe_attachment(
        {"attachment_id": attachment_id}
    )
    async with scoped_session(maker, OWNER) as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT text, confidence FROM app.attachment_extracts"
                        " WHERE attachment_id = :aid AND kind = 'transcript'"
                    ),
                    {"aid": attachment_id},
                )
            )
            .one()
            ._mapping
        )
    assert row["text"] == "" and row["confidence"] == pytest.approx(0.0)

    # Re-ingest sees the cache and enqueues no new job.
    await ingest(maker, blobs).ingest_note({"note_id": note_id})
    assert await transcribe_jobs_for(maker, attachment_id) == 0
