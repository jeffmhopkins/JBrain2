"""The scanned-PDF vision-OCR path end-to-end (docs/plans/EMR_IMPORT_PLAN.md §6.2)
against real Postgres. A text-LESS (scanned) ARIA PDF is attached to a health note
and ingested; the `emr_parse` job finds no text layer, vision-OCRs each page (the
model faked), caches the transcripts as `ocr` extracts, and feeds the page text to
the ARIA parser — whose reads, with no precise source to corroborate them, park in
review (§6.4). A re-run never re-OCRs.
"""

import re
import uuid
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.emr.import_handler import EmrImportPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.notes import Attachment
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_ARIA = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "aria_ocr.txt"


def _aria_pages() -> list[str]:
    """The ARIA fixture's per-page text (the marker lines stripped) — what a vision
    model would return transcribing each scanned page."""
    parts = re.split(r"^--- page \d+ ---$", _ARIA.read_text(), flags=re.M)
    return [p.strip("\n") for p in parts if p.strip()]


def _blank_pdf(pages: int) -> bytes:
    """A text-LESS PDF (blank pages) — get_text('text') yields nothing, so the
    handler must fall back to vision-OCR."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


def _ocr_pipeline(maker, page_texts: list[str]) -> tuple[AnalysisPipeline, FakeLlmClient]:  # noqa: F811
    fake = FakeLlmClient(responses=page_texts)
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "vision.ocr": ("xai", "grok-vision")},
        tiers={"vision": ("xai", "grok-vision")},
    )
    return AnalysisPipeline(maker, router), fake


async def _attach(maker, blobs, note_id: str, data: bytes) -> uuid.UUID:  # noqa: F811
    sha = await blobs.put(data)
    att_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        s.add(
            Attachment(
                id=att_id,
                note_id=uuid.UUID(note_id),
                domain_code="health",
                sha256=sha,
                filename="aria_scan.pdf",
                media_type="application/pdf",
                size_bytes=len(data),
            )
        )
    return att_id


async def test_scanned_aria_pdf_is_ocred_parsed_and_cached(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    pages = _aria_pages()
    assert len(pages) == 2  # the fixture is a two-page reprint
    note_id = await make_note(maker, domain="health", body="Scanned ARIA labs.")
    att_id = await _attach(maker, blobs, note_id, _blank_pdf(len(pages)))
    await ingest(maker, note_id, tmp_path)  # a scanned PDF yields no text-layer chunks

    pipeline, fake = _ocr_pipeline(maker, pages)
    await EmrImportPipeline(maker, blobs, pipeline).parse({"note_id": note_id})

    # One vision.ocr call per page — the text layer was empty, so OCR ran.
    assert len(fake.calls) == 2
    async with scoped_session(maker, SYSTEM_CTX) as s:
        # The transcripts cached as per-page `ocr` extracts (page-anchored, 0.7).
        rows = (
            await s.execute(
                text(
                    "SELECT source_anchor, confidence FROM app.attachment_extracts"
                    " WHERE attachment_id = :a AND kind = 'ocr' ORDER BY source_anchor"
                ),
                {"a": str(att_id)},
            )
        ).all()
        assert {r[0] for r in rows} == {"page 1", "page 2"}
        assert all(abs(float(r[1]) - 0.7) < 1e-6 for r in rows)  # OCR_CONFIDENCE

        # The OCR text reached the ARIA parser: its reads have no precise source to
        # corroborate them, so they park (§6.4) — incl. the page-2 potassium.
        cards = (
            await s.execute(
                text(
                    "SELECT payload->>'analyte' FROM app.review_items"
                    " WHERE kind = 'low_confidence' AND payload->>'subkind' = 'ocr_unreconciled'"
                    " AND payload->>'note_id' = :n"
                ),
                {"n": note_id},
            )
        ).all()
        analytes = {c[0] for c in cards}
        assert "Potassium" in analytes and "Platelet count" in analytes


async def test_re_run_does_not_re_ocr(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    pages = _aria_pages()
    note_id = await make_note(maker, domain="health", body="Scanned ARIA labs.")
    await _attach(maker, blobs, note_id, _blank_pdf(len(pages)))
    await ingest(maker, note_id, tmp_path)

    pipeline, fake = _ocr_pipeline(maker, pages)
    handler = EmrImportPipeline(maker, blobs, pipeline)
    await handler.parse({"note_id": note_id})
    calls_after_first = len(fake.calls)
    await handler.parse({"note_id": note_id})  # cached OCR -> no second vision pass
    assert len(fake.calls) == calls_after_first
