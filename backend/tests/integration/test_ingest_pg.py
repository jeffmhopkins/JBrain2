"""Chunks RLS isolation (CLAUDE.md rule 3) and the ingest pipeline end to end
against real Postgres + pgvector, with blobs in a tmp-dir FsBlobStore."""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pymupdf
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
from jbrain.notes.repo import SqlNotesRepo
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


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


async def make_note(maker: async_sessionmaker[AsyncSession], *, domain: str, body: str) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"ing-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    return note.id


async def add_attachment(
    maker: async_sessionmaker[AsyncSession],
    blobs: FsBlobStore,
    note_id: str,
    *,
    filename: str,
    media_type: str,
    data: bytes,
) -> str:
    digest = await blobs.put(data)
    attachment = await SqlNotesRepo(maker).add_attachment(
        OWNER,
        note_id=note_id,
        sha256=digest,
        filename=filename,
        media_type=media_type,
        size_bytes=len(data),
    )
    assert attachment is not None
    return attachment.id


def pdf_bytes(*page_texts: str) -> bytes:
    doc = pymupdf.open()
    for page_text in page_texts:
        doc.new_page().insert_text((72, 72), page_text)
    return doc.tobytes()


async def chunk_rows(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, note_id: str
) -> list[dict]:
    async with scoped_session(maker, ctx) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, granularity, source_kind, source_anchor, attachment_id,"
                    " char_start, char_end, text, embedding, embedding_model"
                    " FROM app.chunks WHERE note_id = :nid ORDER BY seq"
                ),
                {"nid": note_id},
            )
        ).all()
        return [dict(r._mapping) for r in rows]


async def test_chunks_domain_firewall(maker: async_sessionmaker[AsyncSession]) -> None:
    """Rule 3: every new table proves a scoped session cannot cross domains."""
    health_note = await make_note(maker, domain="health", body="BP was 118 over 76 today")
    general_note = await make_note(maker, domain="general", body="pick up the dry cleaning")
    async with scoped_session(maker, OWNER) as session:
        for note_id, domain, body in (
            (health_note, "health", "BP was 118 over 76 today"),
            (general_note, "general", "pick up the dry cleaning"),
        ):
            await session.execute(
                text(
                    "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                    " VALUES (gen_random_uuid(), :nid, :dom, 'paragraph', 0, :txt)"
                ),
                {"nid": note_id, "dom": domain, "txt": body},
            )

    assert len(await chunk_rows(maker, HEALTH_ONLY, health_note)) == 1
    assert await chunk_rows(maker, GENERAL_ONLY, health_note) == []
    assert await chunk_rows(maker, UNSCOPED, health_note) == []
    assert len(await chunk_rows(maker, OWNER, health_note)) == 1

    health_texts = [c["text"] for c in await chunk_rows(maker, HEALTH_ONLY, health_note)]
    assert "BP was 118 over 76 today" in health_texts

    # A scoped writer cannot smuggle chunks into another domain.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with scoped_session(maker, GENERAL_ONLY) as session:
            await session.execute(
                text(
                    "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                    " VALUES (gen_random_uuid(), :nid, 'health', 'paragraph', 1, 'sneaky')"
                ),
                {"nid": health_note},
            )


async def test_pipeline_ingests_note_with_attachments(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    body = "Grocery planning for the week.\n\nNeed flour, eggs, and maple syrup for pancakes."
    note_id = await make_note(maker, domain="general", body=body)
    txt_att = await add_attachment(
        maker,
        blobs,
        note_id,
        filename="list.txt",
        media_type="text/plain",
        data=b"oat milk and coffee beans",
    )
    pdf_att = await add_attachment(
        maker,
        blobs,
        note_id,
        filename="recipe.pdf",
        media_type="application/pdf",
        data=pdf_bytes("pancake recipe with maple syrup", "second page about toppings"),
    )
    # Unrouted media must be skipped, not fatal.
    await add_attachment(
        maker, blobs, note_id, filename="pic.png", media_type="image/png", data=b"\x89PNG..."
    )

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as session:
        state, indexed_at = (
            await session.execute(
                text("SELECT ingest_state, indexed_at FROM app.notes WHERE id = :id"),
                {"id": note_id},
            )
        ).one()
    assert state == "indexed"
    assert indexed_at is not None

    chunks = await chunk_rows(maker, OWNER, note_id)
    by_kind = {c["source_kind"] for c in chunks}
    assert by_kind == {"note", "text-layer"}

    note_chunks = [c for c in chunks if c["source_kind"] == "note"]
    assert all(c["attachment_id"] is None for c in note_chunks)
    assert any("maple syrup" in c["text"] for c in note_chunks)
    # Spans index into the note body exactly.
    for c in note_chunks:
        assert body[c["char_start"] : c["char_end"]] == c["text"]

    txt_chunks = [c for c in chunks if str(c["attachment_id"]) == txt_att]
    assert len(txt_chunks) == 1
    assert txt_chunks[0]["text"] == "oat milk and coffee beans"

    pdf_chunks = [c for c in chunks if str(c["attachment_id"]) == pdf_att]
    assert {c["source_anchor"] for c in pdf_chunks} == {"page 1", "page 2"}

    # Embeddings stay NULL until Step 3 fills them.
    assert all(c["embedding"] is None and c["embedding_model"] is None for c in chunks)

    # FTS works immediately via the generated tsv column.
    async with scoped_session(maker, OWNER) as session:
        hits = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.chunks WHERE note_id = :nid"
                    " AND tsv @@ plainto_tsquery('english', 'maple syrup')"
                ),
                {"nid": note_id},
            )
        ).scalar()
    assert hits and hits >= 2  # note body + pdf page 1


async def test_reingestion_replaces_chunks(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker, domain="health", body="morning BP reading 120 over 80")
    pipeline = IngestPipeline(maker, blobs)
    await pipeline.ingest_note({"note_id": note_id})
    first = await chunk_rows(maker, OWNER, note_id)

    await add_attachment(
        maker,
        blobs,
        note_id,
        filename="labs.txt",
        media_type="text/plain",
        data=b"cholesterol within range",
    )
    await pipeline.ingest_note({"note_id": note_id})
    second = await chunk_rows(maker, OWNER, note_id)

    # Old chunks are gone (ids not stable across re-ingestion, by design),
    # the body chunk is rebuilt, and the new attachment is now indexed.
    assert {c["id"] for c in first}.isdisjoint({c["id"] for c in second})
    assert sum(1 for c in second if c["source_kind"] == "note") == len(first)
    assert any(c["text"] == "cholesterol within range" for c in second)


async def test_pipeline_failure_marks_note_failed(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    note_id = await make_note(maker, domain="general", body="note with a lost attachment")
    # Register an attachment whose blob was never stored: extraction must fail.
    await SqlNotesRepo(maker).add_attachment(
        OWNER,
        note_id=note_id,
        sha256="00" * 32,
        filename="ghost.txt",
        media_type="text/plain",
        size_bytes=5,
    )

    with pytest.raises(FileNotFoundError):
        await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as session:
        state = (
            await session.execute(
                text("SELECT ingest_state FROM app.notes WHERE id = :id"), {"id": note_id}
            )
        ).scalar()
    assert state == "failed"
    assert await chunk_rows(maker, OWNER, note_id) == []


async def test_pipeline_skips_missing_note(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    # A note deleted between enqueue and claim is a clean no-op.
    await IngestPipeline(maker, blobs).ingest_note({"note_id": str(uuid.uuid4())})
