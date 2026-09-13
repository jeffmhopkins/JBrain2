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
from jbrain.analysis.converse import NOTE_CONVERSE_SPEC
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.ocr import MAX_OCR_BYTES, OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.notes.repo import SqlNotesRepo
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from jbrain.workflow import dispatcher
from jbrain.workflow.registry import ACTION_SPECS, build_registry
from jbrain.workflow.runlog import PipelineRunLog
from jbrain.workflow.scheduler import PURGE_ACTION
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


def _registry():  # noqa: ANN202
    # note.ingested drives the note conversation as well as integration since
    # migration 0194; without its spec the dispatcher cannot resolve that pipeline and
    # the whole tick errors before it enqueues anything.
    return build_registry((*ACTION_SPECS, PURGE_ACTION, NOTE_CONVERSE_SPEC))


async def _seed_owner_principal(maker: async_sessionmaker[AsyncSession]) -> None:
    """A real owner principal so ingest's worker-side note.ingested emit (which has
    no per-content principal) can resolve one — without it emit_event short-circuits
    with 'no owner principal' and the dispatcher has no event to drive integration."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (gen_random_uuid(), 'owner', :kh) ON CONFLICT DO NOTHING"
            ),
            {"kh": f"gate-{uuid.uuid4()}"},
        )


async def tick(maker: async_sessionmaker[AsyncSession]) -> None:
    """W2·C: ingest no longer enqueues integrate directly — it emits note.ingested,
    and the LIVE engine dispatcher resolves that event to the integrate pipeline and
    enqueues the job. The tests drive ingest in-process, so they drive the dispatcher
    in-process too (mirroring the worker loop's tick alongside process_one)."""
    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))


async def make_note(
    maker: async_sessionmaker[AsyncSession],
    body: str = "plain note",
    *,
    attachments_expected: int = 0,
) -> str:
    await _seed_owner_principal(maker)
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER,
        client_id=f"gate-{uuid.uuid4()}",
        domain="general",
        destination=None,
        body=body,
        attachments_expected=attachments_expected,
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
    pipeline = AnalysisPipeline(maker, LlmRouter({"xai": FakeLlmClient([])}, {}))

    async def embed_noop(payload: dict[str, Any]) -> None:
        return None

    async def converse_stub(payload: dict[str, Any]) -> None:
        """What a `note_converse` pass leaves behind, without the agent turn.

        These are GATE tests: what they assert is sequencing — which job is queued,
        when, and how many times — never what a reading produced. Driving a real
        conversation here would put an agent loop, a tool registry and a transcript
        between the assertion and the thing asserted. What has to be real is the mark
        the pass leaves, because the reconciler and the PWA's chip key on it: the
        `note_analysis` stamp (production's own `stamp_analysis`, which every pass that
        read the note runs) and the `integration_state` flip."""
        note_id = uuid.UUID(str(payload["note_id"]))
        async with scoped_session(maker, queue.SYSTEM_CTX) as session:
            await pipeline.stamp_analysis(
                session,
                note_id=note_id,
                note_domain="general",
                title="t",
                tags=["a"],
                extractor="test:converse",
            )
            await session.execute(
                text("UPDATE app.notes SET integration_state = 'integrated' WHERE id = :n"),
                {"n": str(note_id)},
            )

    return {
        "ingest_note": IngestPipeline(maker, blobs).ingest_note,
        "ocr_attachment": OcrPipeline(maker, blobs, vision, SqlSettingsStore(maker)).ocr_attachment,
        "note_converse": converse_stub,
        "embed_note": embed_noop,
    }


async def drain(maker: async_sessionmaker[AsyncSession], h: dict[str, worker.Handler]) -> None:
    """Run the worker to quiescence, ticking the LIVE dispatcher between jobs so a
    note.ingested emitted by ingest (or the OCR handler's re-ingest) is resolved into
    a note_converse job — exactly what the worker loop does in production now that
    integration is engine-driven. Loop until neither a job nor a tick makes progress."""
    while True:
        ran = await worker.process_one(maker, h)
        before = await _undispatched_count(maker)
        await tick(maker)
        dispatched = before > 0
        if not ran and not dispatched:
            return


async def _undispatched_count(maker: async_sessionmaker[AsyncSession]) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(text("SELECT count(*) FROM app.events WHERE dispatched_at IS NULL"))
        ).scalar_one()


async def test_image_note_ingest_enqueues_ocr_but_not_analyze(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker, "photo of the receipt")
    att_id = await add_image(maker, note_id, blobs=blobs)
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    assert await jobs_for(maker, "ocr_attachment", "attachment_id", att_id) == ["queued"]
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == []
    # Embedding stays ungated: keyword/vector search never waits on vision.
    assert await jobs_for(maker, "embed_note", "note_id", note_id) == ["queued"]


async def test_oversized_image_note_analyzes_immediately(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    # Skipped at enqueue with no cache row: never outstanding, never blocks.
    note_id = await make_note(maker, "giant scan attached")
    att_id = await add_image(maker, note_id, blobs=blobs, size_bytes=MAX_OCR_BYTES + 1)
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    await tick(maker)

    assert await jobs_for(maker, "ocr_attachment", "attachment_id", att_id) == []
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["queued"]


async def test_imageless_note_analyzes_immediately(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker)
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    await tick(maker)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["queued"]


async def test_note_expecting_attachment_defers_integration_until_it_lands(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The capture race: the note is ingested BEFORE its promised image uploads.
    Seeing zero attachments and no outstanding OCR, the old gate would emit
    note.ingested and drive a premature body-only integration. The attachment-intent
    hint holds it: no integrate while the promised attachment is absent, then exactly
    one — after OCR — once it lands."""
    await quiesce(maker)
    note_id = await make_note(maker, "car loan photo attached", attachments_expected=1)
    pipeline = IngestPipeline(maker, blobs)

    # First ingest, before the upload: no attachment present, so despite there being
    # no OCR work outstanding the emit is deferred — no premature body-only pass.
    await pipeline.ingest_note({"note_id": note_id})
    await tick(maker)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == []
    # Search never waits: embedding is enqueued even while integration is deferred.
    assert await jobs_for(maker, "embed_note", "note_id", note_id) == ["queued"]

    # The image lands (a second request) and re-ingests: now OCR gates, still no
    # body-only integrate. Then the full chain runs to exactly one extraction.
    await add_image(maker, note_id, blobs=blobs, filename="loan.png")
    h = handlers(
        maker,
        blobs,
        ocr_responses=["APR 4.9% — 60 months", "A car-loan statement."],
        extract_responses=[EMPTY_EXTRACTION],
    )
    await queue.enqueue(maker, OWNER, "ingest_note", {"note_id": note_id})
    await drain(maker, h)

    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["done"]
    async with scoped_session(maker, OWNER) as s:
        analyzed = (
            await s.execute(
                text("SELECT count(*) FROM app.note_analysis WHERE note_id = :nid"),
                {"nid": note_id},
            )
        ).scalar_one()
    assert analyzed == 1


async def test_queued_analyze_dedups_but_running_does_not(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker)
    pipeline = IngestPipeline(maker, blobs)
    # Two ingests, each driven through the engine. The queued twin from the first
    # dispatch dedups the second (the dispatcher's _already_active skips a queued
    # pass) — one job covers the re-ingest, reading the rebuilt note.
    await pipeline.ingest_note({"note_id": note_id})
    await tick(maker)
    await pipeline.ingest_note({"note_id": note_id})
    await tick(maker)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["queued"]

    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.jobs SET status = 'running', locked_at = now()"
                " WHERE kind = 'note_converse' AND payload->>'note_id' = :nid"
            ),
            {"nid": note_id},
        )
    await pipeline.ingest_note({"note_id": note_id})
    await tick(maker)
    # A RUNNING analyze may have read stale chunks: a fresh pass must follow (the
    # dispatcher's queued-only dedup never suppresses behind a running job).
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["running", "queued"]


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

    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["done"]
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
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["done", "done"]


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
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == []
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
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["done"]


async def test_backfill_skips_notes_with_active_ocr(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """A worker restart mid-OCR must not enqueue a premature analyze: the
    note has no note_analysis row yet, but its vision text is still coming.

    The three tests below assert on `note_converse` since R3 — the reconciler enqueues
    the note's graph producer, and that is the conversation now — while the WAIT they
    pin is unchanged, because it is a property of the note and not of the producer."""
    await quiesce(maker)
    note_id = await make_note(maker, "indexed but ocr in flight")
    att_id = await add_image(maker, note_id, blobs=blobs)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET ingest_state = 'indexed' WHERE id = :nid"),
            {"nid": note_id},
        )
    await queue.enqueue(maker, OWNER, "ocr_attachment", {"attachment_id": att_id})

    await queue.backfill_pending_integration(maker, OWNER)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == []

    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.jobs SET status = 'done' WHERE kind = 'ocr_attachment'"
                " AND payload->>'attachment_id' = :aid"
            ),
            {"aid": att_id},
        )
    await queue.backfill_pending_integration(maker, OWNER)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["queued"]


async def test_backfill_waits_for_promised_attachment_then_settles(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The reconciler must honor the same attachment-intent wait as the ingest gate,
    or it would body-only integrate a note during its upload window and defeat the
    gate. A note that still expects an unarrived attachment is skipped — until the
    settle window lapses (a promise that never came), when it integrates on what it
    has so a failed upload can't strand it forever."""
    await quiesce(maker)
    note_id = await make_note(maker, "promised an image", attachments_expected=1)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET ingest_state = 'indexed' WHERE id = :nid"),
            {"nid": note_id},
        )

    # No attachment has landed yet and the note is fresh: within the settle window,
    # the reconciler leaves it alone.
    await queue.backfill_pending_integration(maker, OWNER)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == []

    # Backdate RECEIPT past the settle window — the promise never arrived, so the
    # note becomes eligible and integrates body-only rather than stranding.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.notes SET received_at = now() - make_interval("
                "secs => :secs) WHERE id = :nid"
            ),
            {"secs": queue.INTEGRATION_ATTACHMENT_SETTLE_SECONDS + 60, "nid": note_id},
        )
    await queue.backfill_pending_integration(maker, OWNER)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["queued"]


async def test_offline_flushed_note_still_gets_its_attachment_window(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The settle window measures the UPLOAD race, so it must run from the server's
    receipt instant, not the client's capture time.

    `notes.created_at` is the client's (notes/repo.py: "Client capture time wins when
    supplied — the offline outbox flushes later"), so a note captured yesterday and
    flushed now arrives already past a created_at window — defeating the gate in
    exactly the case it exists for, and body-only integrating a note whose promised
    image is still uploading. Against `received_at` the window is honored, and it
    still lapses on its own terms.
    """
    await quiesce(maker)
    note_id = await make_note(maker, "captured offline, flushed today", attachments_expected=1)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.notes SET ingest_state = 'indexed',"
                " created_at = now() - interval '1 day' WHERE id = :nid"
            ),
            {"nid": note_id},
        )

    await queue.backfill_pending_integration(maker, OWNER)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == []

    # The promised attachment lands inside the window: it integrates WITH the image.
    await add_image(maker, note_id, blobs=blobs)
    await queue.backfill_pending_integration(maker, OWNER)
    assert await jobs_for(maker, "note_converse", "note_id", note_id) == ["queued"]
