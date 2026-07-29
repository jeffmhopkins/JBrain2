"""The scanned-PDF page-OCR path against real Postgres (docs/plans/EMR_IMPORT_PLAN.md
§6.2): a text-less PDF is transcribed page by page by `ocr_attachment` (per-page
`ocr` extracts, no caption), ingest auto-enqueues that OCR for a scanned PDF, and a
re-ingest turns the transcripts into per-page `ocr` chunks. The vision LLM is faked.
"""

import uuid

import pymupdf
import pytest
from sqlalchemy import text

from jbrain.db.session import scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_ocr_pg import (  # noqa: F401
    extract_rows,
    maker,
    ocr_pipeline,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _blank_pdf(pages: int) -> bytes:
    """A text-LESS PDF — get_text('text') yields nothing, so it is treated as a scan."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


async def _note_with_pdf(maker, blobs, data: bytes) -> tuple[str, str]:  # noqa: F811
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"pdf-{uuid.uuid4()}", domain="health", destination=None, body="scan"
    )
    sha = await blobs.put(data)
    att = await repo.add_attachment(
        OWNER,
        note_id=note.id,
        sha256=sha,
        filename="records.pdf",
        media_type="application/pdf",
        size_bytes=len(data),
    )
    assert att is not None
    return note.id, att.id


async def _ocr_job_count(maker, att_id: str) -> int:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ocr_attachment'"
                    " AND payload->>'attachment_id' = :a"
                ),
                {"a": att_id},
            )
        ).scalar_one()


async def test_ocr_attachment_transcribes_a_pdf_per_page(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    _, att_id = await _note_with_pdf(maker, blobs, _blank_pdf(2))
    fake = FakeLlmClient(responses=["PAGE ONE TEXT", "PAGE TWO TEXT"])

    await ocr_pipeline(maker, blobs, fake).ocr_attachment({"attachment_id": att_id})

    rows = await extract_rows(maker, att_id)
    ocr = [r for r in rows if r["kind"] == "ocr"]
    assert {r["source_anchor"] for r in ocr} == {"page 1", "page 2"}  # per-page anchors
    assert {r["text"] for r in ocr} == {"PAGE ONE TEXT", "PAGE TWO TEXT"}
    assert not [r for r in rows if r["kind"] == "caption"]  # a document isn't captioned
    assert len(fake.calls) == 2  # one vision.ocr call per page


async def test_ingest_auto_enqueues_ocr_for_a_scanned_pdf(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    note_id, att_id = await _note_with_pdf(maker, blobs, _blank_pdf(1))

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    assert await _ocr_job_count(maker, att_id) == 1  # a scanned PDF routes to page-OCR


async def test_scanned_pdf_ocr_becomes_per_page_chunks(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    note_id, att_id = await _note_with_pdf(maker, blobs, _blank_pdf(2))
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})  # enqueues OCR

    fake = FakeLlmClient(responses=["PLATELET 38", "POTASSIUM 4.8"])
    await ocr_pipeline(maker, blobs, fake).ocr_attachment({"attachment_id": att_id})
    # The OCR handler re-enqueued ingest_note; drain it to build the chunks.
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    async with scoped_session(maker, OWNER) as s:
        chunks = (
            await s.execute(
                text(
                    "SELECT source_anchor, text FROM app.chunks"
                    " WHERE note_id = :n AND source_kind = 'ocr' ORDER BY source_anchor"
                ),
                {"n": note_id},
            )
        ).all()
    assert {c[0] for c in chunks} == {"page 1", "page 2"}  # searchable, per-page
    assert any("PLATELET 38" in c[1] for c in chunks)
    # A re-ingest of the now-cached PDF does not re-enqueue OCR (no loop).
    assert await _ocr_job_count(maker, att_id) == 1
