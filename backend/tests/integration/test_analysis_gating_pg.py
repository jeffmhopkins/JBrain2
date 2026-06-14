"""The analyze_note gate against real Postgres: ingest defers analysis while
vision work is outstanding (and ONLY then), the OCR handler's re-ingest closes
the loop with exactly one extraction, retry exhaustion falls back to body-only
analysis, and the startup backfill respects in-flight OCR. LLMs always faked."""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain import queue, worker
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.ocr import MAX_OCR_BYTES, OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.notes.repo import SqlNotesRepo
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

EMPTY_EXTRACTION = json.dumps(
    {"title": "t", "tags": ["a", "b", "c"], "mentions": [], "facts": [], "temporal_tokens": []}
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


@pytest.fixture(autouse=True)
async def _pin_v1_pipeline(maker: async_sessionmaker[AsyncSession]) -> None:  # noqa: F811
    # This suite asserts the v1 analyze_note gate. integrate is now the default
    # pipeline, so pin the toggle to analyze explicitly; the full cutover migrates
    # these onto integrate_note (docs/CUTOVER_V1_REMOVAL.md).
    from jbrain.settings_store import NOTE_PIPELINE_KEY

    await SqlSettingsStore(maker).upsert(queue.SYSTEM_CTX, NOTE_PIPELINE_KEY, "analyze")


async def make_note(maker: async_sessionmaker[AsyncSession], body: str = "plain note") -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"gate-{uuid.uuid4()}", domain="general", destination=None, body=body
    )
    return note.id


async def add_image(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    *,
    blobs: FsBlobStore | None = None,
    data: bytes = b"\x89PNG fake",
    size_bytes: int | None = None,
    filename: str = "pic.png",
) -> str:
    # No blob store given = a deliberately dangling sha (per-file, so two
    # lost attachments don't collide) — the OCR handler's blob read fails.
    digest = await blobs.put(data) if blobs is not None else uuid.uuid4().hex * 2
    att = await SqlNotesRepo(maker).add_attachment(
        OWNER,
        note_id=note_id,
        sha256=digest,
        filename=filename,
        media_type="image/png",
        size_bytes=size_bytes if size_bytes is not None else len(data),
    )
    assert att is not None
    return att.id


async def jobs_for(
    maker: async_sessionmaker[AsyncSession], kind: str, field: str, value: str
) -> list[str]:
    """Statuses of every `kind` job whose payload field matches, oldest first."""
    async with scoped_session(maker, OWNER) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT status FROM app.jobs WHERE kind = :kind"
                        " AND payload->>:field = :value ORDER BY created_at"
                    ),
                    {"kind": kind, "field": field, "value": value},
                )
            ).scalars()
        )


async def quiesce(maker: async_sessionmaker[AsyncSession]) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.jobs SET status = 'done' WHERE status = 'queued'"))


def handlers(
    maker: async_sessionmaker[AsyncSession],
    blobs: FsBlobStore,
    *,
    ocr_responses: list[str],
    extract_responses: list[str],
) -> dict[str, worker.Handler]:
    """The worker's handler table with every LLM faked and embedding stubbed
    (a dead embed container must never block this flow in production either)."""
    vision = LlmRouter(
        {"xai": FakeLlmClient(ocr_responses)},
        {"vision.ocr": ("xai", "grok-4.3"), "vision.caption": ("xai", "grok-4.3")},
    )
    extract = LlmRouter(
        {"xai": FakeLlmClient(extract_responses)}, {"note.extract": ("xai", "grok-4.3")}
    )

    async def embed_noop(payload: dict[str, Any]) -> None:
        return None

    return {
        "ingest_note": IngestPipeline(maker, blobs).ingest_note,
        "ocr_attachment": OcrPipeline(maker, blobs, vision, SqlSettingsStore(maker)).ocr_attachment,
        "analyze_note": AnalysisPipeline(maker, extract).analyze_note,
        "embed_note": embed_noop,
    }


async def drain(maker: async_sessionmaker[AsyncSession], h: dict[str, worker.Handler]) -> None:
    while await worker.process_one(maker, h):
        pass


async def test_image_note_ingest_enqueues_ocr_but_not_analyze(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker, "photo of the receipt")
    att_id = await add_image(maker, note_id, blobs=blobs)
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    assert await jobs_for(maker, "ocr_attachment", "attachment_id", att_id) == ["queued"]
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == []
    # Embedding stays ungated: keyword/vector search never waits on vision.
    assert await jobs_for(maker, "embed_note", "note_id", note_id) == ["queued"]


async def test_oversized_image_note_analyzes_immediately(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    # Skipped at enqueue with no cache row: never outstanding, never blocks.
    note_id = await make_note(maker, "giant scan attached")
    att_id = await add_image(maker, note_id, blobs=blobs, size_bytes=MAX_OCR_BYTES + 1)
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    assert await jobs_for(maker, "ocr_attachment", "attachment_id", att_id) == []
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["queued"]


async def test_imageless_note_analyzes_immediately(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker)
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["queued"]


async def test_queued_analyze_dedups_but_running_does_not(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker)
    pipeline = IngestPipeline(maker, blobs)
    await pipeline.ingest_note({"note_id": note_id})
    await pipeline.ingest_note({"note_id": note_id})
    # A queued job covers the re-ingest: it will read the rebuilt chunks.
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["queued"]

    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.jobs SET status = 'running', locked_at = now()"
                " WHERE kind = 'analyze_note' AND payload->>'note_id' = :nid"
            ),
            {"nid": note_id},
        )
    await pipeline.ingest_note({"note_id": note_id})
    # A RUNNING analyze may have read stale chunks: a fresh pass must follow.
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["running", "queued"]


async def test_full_chain_runs_exactly_one_analysis(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """ingest -> OCR -> re-ingest -> analyze, end to end through the worker:
    one extraction ever, and it sees the OCR text."""
    await quiesce(maker)
    note_id = await make_note(maker, "kept the receipt")
    await add_image(maker, note_id, blobs=blobs, filename="receipt.png")
    h = handlers(
        maker,
        blobs,
        ocr_responses=["Total: $41.20", "A crumpled receipt."],
        extract_responses=[EMPTY_EXTRACTION],
    )
    await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": note_id})
    await drain(maker, h)

    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["done"]
    async with scoped_session(maker, OWNER) as s:
        analyzed = (
            await s.execute(
                text("SELECT count(*) FROM app.note_analysis WHERE note_id = :nid"),
                {"nid": note_id},
            )
        ).scalar_one()
    assert analyzed == 1


async def test_on_demand_analyze_of_cached_attachment_does_not_deadlock(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The re-run path: an on-demand full analysis of an already-cached
    attachment re-describes, re-ingests, and analysis still follows — the
    gate keys on WORK, and once the job finishes none is outstanding."""
    note_id = await make_note(maker, "receipt photo")
    att_id = await add_image(maker, note_id, blobs=blobs, filename="receipt.png")
    h = handlers(
        maker,
        blobs,
        ocr_responses=["Total: $41.20", "A crumpled receipt."],
        extract_responses=[EMPTY_EXTRACTION],
    )
    await quiesce(maker)
    await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": note_id})
    await drain(maker, h)
    await quiesce(maker)

    # What POST /attachments/{id}/analyze enqueues (the cache row suppresses
    # a second transcription; only the description re-runs).
    h2 = handlers(
        maker,
        blobs,
        ocr_responses=["A flattened receipt in daylight."],
        extract_responses=[EMPTY_EXTRACTION],
    )
    await queue.enqueue(maker, OWNER, "ocr_attachment", {"attachment_id": att_id, "mode": "full"})
    await drain(maker, h2)

    assert await jobs_for(maker, "ocr_attachment", "attachment_id", att_id) == ["done", "done"]
    # One analysis per pass — the first from the initial chain, the second
    # following the on-demand re-describe — and neither deadlocked.
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["done", "done"]


async def test_ocr_exhaustion_falls_back_to_body_only_analysis(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """Two cache-less attachments whose blobs are gone: both OCR jobs exhaust,
    and exactly ONE fallback analyze lands — the first exhaustion defers to
    the second still-active job, the second enqueues."""
    await quiesce(maker)
    note_id = await make_note(maker, "two lost photos")
    await add_image(maker, note_id, filename="one.png")  # no blob stored
    await add_image(maker, note_id, filename="two.png")
    h = handlers(maker, blobs, ocr_responses=["unused"], extract_responses=[EMPTY_EXTRACTION])
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == []
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.jobs SET max_attempts = 1 WHERE kind = 'ocr_attachment'"))
    await drain(maker, h)

    async with scoped_session(maker, OWNER) as s:
        failed = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ocr_attachment'"
                    " AND status = 'failed'"
                )
            )
        ).scalar_one()
    assert failed == 2  # the failed rows stay the durable record
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["done"]


async def test_backfill_skips_notes_with_active_ocr(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """A worker restart mid-OCR must not enqueue a premature analyze: the
    note has no note_analysis row yet, but its vision text is still coming."""
    await quiesce(maker)
    note_id = await make_note(maker, "indexed but ocr in flight")
    att_id = await add_image(maker, note_id, blobs=blobs)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET ingest_state = 'indexed' WHERE id = :nid"),
            {"nid": note_id},
        )
    await queue.enqueue(maker, OWNER, "ocr_attachment", {"attachment_id": att_id})

    await queue.backfill_unanalyzed_notes(maker, OWNER)
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == []

    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.jobs SET status = 'done' WHERE kind = 'ocr_attachment'"
                " AND payload->>'attachment_id' = :aid"
            ),
            {"aid": att_id},
        )
    await queue.backfill_unanalyzed_notes(maker, OWNER)
    assert await jobs_for(maker, "analyze_note", "note_id", note_id) == ["queued"]
