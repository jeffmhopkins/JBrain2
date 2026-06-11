"""Migration 0010 + the vision-OCR attachment chain against real Postgres:
attachment_extracts RLS isolation (CLAUDE.md rule 3) and the ocr_attachment
round trip (blob -> faked vision text -> extract rows -> re-ingest builds
ocr/caption chunks -> FTS finds the text), with the LLM always faked. Also
the image-analysis modes: full = OCR + description calls, ocr = OCR only,
and the on-demand payload override that re-describes without re-billing."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.ocr import DESCRIPTION_SYSTEM, MAX_OCR_BYTES, OCR_SYSTEM, OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.notes.repo import SqlNotesRepo
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_note_with_image(
    maker: async_sessionmaker[AsyncSession],
    blobs: FsBlobStore | None = None,
    *,
    domain: str = "health",
    body: str = "scanned the lab slip",
    filename: str = "labs.png",
    data: bytes = b"\x89PNG fake image bytes",
    size_bytes: int | None = None,
) -> tuple[str, str]:
    """A note + one image attachment; returns (note_id, attachment_id)."""
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"ocr-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    digest = await blobs.put(data) if blobs is not None else "00" * 32
    attachment = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256=digest,
        filename=filename,
        media_type="image/png",
        size_bytes=size_bytes if size_bytes is not None else len(data),
    )
    assert attachment is not None
    return note.id, attachment.id


async def insert_extract(
    maker: async_sessionmaker[AsyncSession],
    attachment_id: str,
    *,
    domain: str = "health",
    kind: str = "ocr",
    body: str = "Glucose 92 mg/dL",
) -> str:
    extract_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.attachment_extracts"
                " (id, attachment_id, kind, tool, text, confidence, source_anchor, domain_code)"
                " VALUES (:id, :aid, :kind, 'fake:model', :txt, 0.7, 'labs.png', :dom)"
            ),
            {"id": extract_id, "aid": attachment_id, "kind": kind, "txt": body, "dom": domain},
        )
    return extract_id


async def count_visible(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, extract_id: str
) -> int:
    async with scoped_session(maker, ctx) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.attachment_extracts WHERE id = :id"),
                {"id": extract_id},
            )
        ).scalar_one()


async def test_attachment_extracts_enforce_domain_firewall(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Rule 3: a health-domain OCR extract is invisible without health scope."""
    _, attachment_id = await make_note_with_image(maker)
    extract_id = await insert_extract(maker, attachment_id)

    assert await count_visible(maker, HEALTH_ONLY, extract_id) == 1
    assert await count_visible(maker, OWNER, extract_id) == 1
    assert await count_visible(maker, GENERAL_ONLY, extract_id) == 0
    assert await count_visible(maker, UNSCOPED, extract_id) == 0


async def test_scoped_writer_cannot_smuggle_extracts_across_domains(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    _, attachment_id = await make_note_with_image(maker)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.attachment_extracts"
                    " (id, attachment_id, kind, tool, text, domain_code)"
                    " VALUES (gen_random_uuid(), :aid, 'ocr', 'fake:model', 'sneaky', 'health')"
                ),
                {"aid": attachment_id},
            )


async def test_extract_rows_cascade_with_their_attachment(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    _, attachment_id = await make_note_with_image(maker)
    extract_id = await insert_extract(maker, attachment_id)
    assert await SqlNotesRepo(maker).remove_attachment(OWNER, attachment_id) is not None
    assert await count_visible(maker, OWNER, extract_id) == 0


# --- the ocr_attachment round trip ------------------------------------------


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


def vision_router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter(
        {"xai": fake},
        {"vision.ocr": ("xai", "grok-4.3"), "vision.caption": ("xai", "grok-4.3")},
    )


def ocr_pipeline(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore, fake: FakeLlmClient
) -> OcrPipeline:
    """The handler wired like the worker: real store, faked LLM."""
    return OcrPipeline(maker, blobs, vision_router(fake), SqlSettingsStore(maker))


async def ocr_jobs_for(maker: async_sessionmaker[AsyncSession], attachment_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ocr_attachment'"
                    " AND payload->>'attachment_id' = :aid"
                ),
                {"aid": attachment_id},
            )
        ).scalar_one()


async def extract_rows(maker: async_sessionmaker[AsyncSession], attachment_id: str) -> list[dict]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT kind, tool, text, confidence, source_anchor"
                    " FROM app.attachment_extracts WHERE attachment_id = :aid ORDER BY kind"
                ),
                {"aid": attachment_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def test_ocr_round_trip_blob_to_searchable_chunks(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_image(
        maker, blobs, body="kept the receipt", filename="receipt.png", domain="general"
    )
    pipeline = IngestPipeline(maker, blobs)

    # Ingest is LLM-free: it only enqueues the OCR job for the uncached image.
    await pipeline.ingest_note({"note_id": note_id})
    assert await ocr_jobs_for(maker, attachment_id) == 1
    note = await SqlNotesRepo(maker).get_note(OWNER, note_id)
    assert note is not None and note.attachments[0].has_extracts is False  # "ocr queued…"
    # Enqueue-once: a re-ingest while the job is still queued adds nothing.
    await pipeline.ingest_note({"note_id": note_id})
    assert await ocr_jobs_for(maker, attachment_id) == 1

    fake = FakeLlmClient(["Total: $41.20\nThanks for shopping", "A crumpled grocery receipt."])
    await ocr_pipeline(maker, blobs, fake).ocr_attachment({"attachment_id": attachment_id})

    # Default mode is full [decided]: exactly one OCR call and one
    # description call, each carrying the image.
    assert [c["system"] for c in fake.calls] == [OCR_SYSTEM, DESCRIPTION_SYSTEM]
    for call in fake.calls:
        assert len(call["images"]) == 1
        assert call["images"][0].media_type == "image/png"

    rows = await extract_rows(maker, attachment_id)
    assert [(r["kind"], r["tool"], r["source_anchor"]) for r in rows] == [
        ("caption", "xai:grok-4.3", "receipt.png"),
        ("ocr", "xai:grok-4.3", "receipt.png"),
    ]
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["ocr"]["confidence"] == pytest.approx(0.7)  # the Guards cap
    assert by_kind["caption"]["confidence"] == pytest.approx(0.6)
    # The API's chip signal flips once the cache fills ("text extracted").
    note = await SqlNotesRepo(maker).get_note(OWNER, note_id)
    assert note is not None and note.attachments[0].has_extracts is True

    # The handler re-enqueued ingest; run it and the cache becomes chunks.
    await pipeline.ingest_note({"note_id": note_id})
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
                    " AND tsv @@ plainto_tsquery('english', 'grocery receipt')"
                ),
                {"nid": note_id},
            )
        ).scalar_one()
    assert {(c.source_kind, c.source_anchor) for c in chunks} == {
        ("ocr", "receipt.png"),
        ("caption", "receipt.png"),
    }
    assert any("Total: $41.20" in c.text for c in chunks)
    assert hits >= 1  # FTS finds the vision text immediately

    # The cache suppresses re-OCR: this re-ingest enqueues no new job.
    assert await ocr_jobs_for(maker, attachment_id) == 1


async def test_analyze_prompt_marks_ocr_chunks_so_the_model_knows(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The extraction call must SEE which text is machine-read: OCR chunks
    reach note.extract prefixed with their provenance marker (Guards)."""
    from jbrain.analysis.pipeline import AnalysisPipeline

    note_id, attachment_id = await make_note_with_image(
        maker, blobs, body="filed the receipt", filename="receipt.png", domain="general"
    )
    pipeline = IngestPipeline(maker, blobs)
    await pipeline.ingest_note({"note_id": note_id})
    await ocr_pipeline(
        maker, blobs, FakeLlmClient(["Total: $41.20", "A grocery receipt."])
    ).ocr_attachment({"attachment_id": attachment_id})
    await pipeline.ingest_note({"note_id": note_id})

    extract_fake = FakeLlmClient(
        [
            '{"title": "t", "tags": ["a", "b", "c"], "mentions": [], "facts": [],'
            ' "temporal_tokens": []}'
        ]
    )
    analyzer = AnalysisPipeline(
        maker, LlmRouter({"xai": extract_fake}, {"note.extract": ("xai", "grok-4.3")})
    )
    await analyzer.analyze_note({"note_id": note_id})

    user_text = extract_fake.calls[0]["user_text"]
    assert "[ocr from receipt.png]\nTotal: $41.20" in user_text
    assert "[image caption of receipt.png]\nA grocery receipt." in user_text
    assert "filed the receipt" in user_text  # body chunk stays unmarked


async def test_ingest_skips_ocr_for_oversized_images(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_image(
        maker, blobs, filename="huge.png", size_bytes=MAX_OCR_BYTES + 1
    )
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    assert await ocr_jobs_for(maker, attachment_id) == 0


async def test_ocr_handler_noops_when_attachment_or_note_is_gone(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    fake = FakeLlmClient()
    handler = ocr_pipeline(maker, blobs, fake)
    await handler.ocr_attachment({"attachment_id": str(uuid.uuid4())})

    note_id, attachment_id = await make_note_with_image(maker, blobs)
    assert await SqlNotesRepo(maker).delete_note(OWNER, note_id)
    await handler.ocr_attachment({"attachment_id": attachment_id})

    assert fake.calls == []  # neither skip path may bill a vision call
    assert await extract_rows(maker, attachment_id) == []


async def test_illegible_image_writes_empty_cache_rows_and_no_chunks(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id, attachment_id = await make_note_with_image(maker, blobs, filename="blur.jpg")
    pipeline = IngestPipeline(maker, blobs)
    await pipeline.ingest_note({"note_id": note_id})

    fake = FakeLlmClient(["", "A blurry photo."])
    await ocr_pipeline(maker, blobs, fake).ocr_attachment({"attachment_id": attachment_id})
    by_kind = {r["kind"]: r for r in await extract_rows(maker, attachment_id)}
    assert by_kind["ocr"]["text"] == ""
    assert by_kind["ocr"]["confidence"] == pytest.approx(0.0)

    await pipeline.ingest_note({"note_id": note_id})
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
    assert kinds == {"caption"}  # the empty OCR row produced no chunk
    # ...but it still suppresses another OCR pass.
    assert await ocr_jobs_for(maker, attachment_id) == 1


# --- image-analysis modes (settings-driven; on-demand payload override) ------


async def chunk_kinds(
    maker: async_sessionmaker[AsyncSession], note_id: str, attachment_id: str
) -> set[str]:
    async with scoped_session(maker, OWNER) as s:
        return set(
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


async def test_ocr_only_mode_skips_the_description_call(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """mode=ocr (read from app.settings per job): one transcription call,
    no caption row, and re-ingest builds only the ocr chunk."""
    store = SqlSettingsStore(maker)
    await store.upsert(OWNER, "image_analysis_mode", "ocr")
    try:
        note_id, attachment_id = await make_note_with_image(
            maker, blobs, body="kept the slip", filename="slip.png", domain="general"
        )
        pipeline = IngestPipeline(maker, blobs)
        await pipeline.ingest_note({"note_id": note_id})

        fake = FakeLlmClient(["Total: $9.50"])
        await ocr_pipeline(maker, blobs, fake).ocr_attachment({"attachment_id": attachment_id})
        assert [c["system"] for c in fake.calls] == [OCR_SYSTEM]

        rows = await extract_rows(maker, attachment_id)
        assert [r["kind"] for r in rows] == ["ocr"]
        # The chip signal still flips: text was extracted, just no description.
        note = await SqlNotesRepo(maker).get_note(OWNER, note_id)
        assert note is not None and note.attachments[0].has_extracts is True

        await pipeline.ingest_note({"note_id": note_id})
        assert await chunk_kinds(maker, note_id, attachment_id) == {"ocr"}
        # The ocr row alone is the cache marker: no re-OCR is enqueued.
        assert await ocr_jobs_for(maker, attachment_id) == 1
    finally:
        await store.upsert(OWNER, "image_analysis_mode", "full")


async def test_on_demand_full_override_round_trip(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The analyze endpoint's job ({mode: "full"}) beats the stored ocr-only
    mode, re-describes WITHOUT re-billing the transcription (delete+insert of
    the caption row only), and re-ingest builds the caption chunk."""
    store = SqlSettingsStore(maker)
    await store.upsert(OWNER, "image_analysis_mode", "ocr")
    try:
        note_id, attachment_id = await make_note_with_image(
            maker, blobs, body="receipt photo", filename="receipt.png", domain="general"
        )
        pipeline = IngestPipeline(maker, blobs)
        await pipeline.ingest_note({"note_id": note_id})
        await ocr_pipeline(maker, blobs, FakeLlmClient(["Total: $41.20"])).ocr_attachment(
            {"attachment_id": attachment_id}
        )
        await pipeline.ingest_note({"note_id": note_id})
        assert await chunk_kinds(maker, note_id, attachment_id) == {"ocr"}

        fake = FakeLlmClient(["A crumpled grocery receipt on a kitchen counter."])
        await ocr_pipeline(maker, blobs, fake).ocr_attachment(
            {"attachment_id": attachment_id, "mode": "full"}
        )
        # Only the description call ran: the OCR cache row already existed.
        assert [c["system"] for c in fake.calls] == [DESCRIPTION_SYSTEM]

        by_kind = {r["kind"]: r for r in await extract_rows(maker, attachment_id)}
        assert set(by_kind) == {"caption", "ocr"}
        assert by_kind["ocr"]["text"] == "Total: $41.20"  # transcription kept
        assert by_kind["caption"]["text"].startswith("A crumpled grocery receipt")
        assert by_kind["caption"]["confidence"] == pytest.approx(0.6)

        # The handler re-enqueued ingest; running it makes the description a
        # searchable caption chunk.
        await pipeline.ingest_note({"note_id": note_id})
        assert await chunk_kinds(maker, note_id, attachment_id) == {"ocr", "caption"}
        async with scoped_session(maker, OWNER) as s:
            hits = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM app.chunks WHERE note_id = :nid"
                        " AND tsv @@ plainto_tsquery('english', 'kitchen counter')"
                    ),
                    {"nid": note_id},
                )
            ).scalar_one()
        assert hits >= 1

        # A second on-demand run is the re-run path: still one caption row.
        fake2 = FakeLlmClient(["A flattened receipt, photographed in daylight."])
        await ocr_pipeline(maker, blobs, fake2).ocr_attachment(
            {"attachment_id": attachment_id, "mode": "full"}
        )
        by_kind = {r["kind"]: r for r in await extract_rows(maker, attachment_id)}
        assert by_kind["caption"]["text"] == "A flattened receipt, photographed in daylight."
        assert len(await extract_rows(maker, attachment_id)) == 2
    finally:
        await store.upsert(OWNER, "image_analysis_mode", "full")
