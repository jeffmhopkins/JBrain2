"""Migration 0010 + the vision-OCR attachment chain against real Postgres:
attachment_extracts RLS isolation (CLAUDE.md rule 3) and the ocr_attachment
round trip (blob -> faked vision text -> extract rows -> re-ingest builds
ocr/caption chunks -> FTS finds the text), with the LLM always faked."""

import uuid
from collections.abc import AsyncIterator

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
