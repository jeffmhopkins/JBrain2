"""The EMR parse+integrate job end-to-end (docs/plans/EMR_IMPORT_PLAN.md §6.3–§6.6)
against real Postgres. A decrypted athena PDF is attached to a health note and
ingested (so each page becomes a cited chunk); the `emr_parse` handler then
extracts → dispatches → parses → integrates, minting graph facts + the
`lab_results` projection, with every fact citing a REAL attachment page chunk.
"""

import uuid
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import func, select, text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.emr.import_handler import EmrImportPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact
from jbrain.models.notes import Attachment, Chunk
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_ATHENA = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "athena_panel.txt"


def _pdf_from_lines(text_body: str) -> bytes:
    """A one-page PDF whose text layer reproduces the fixture's lines (minus the
    `--- page N ---` marker), one line per row — so get_text('text') round-trips the
    structure the parser reads."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=1500)
    y = 40.0
    for line in text_body.splitlines():
        if line.strip().startswith("--- page"):
            continue
        if line.strip():
            page.insert_text((40, y), line, fontsize=9)
        y += 14.0
    out = doc.tobytes()
    doc.close()
    return out


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(maker, router)


async def _attach_pdf(maker, blobs, note_id: str, data: bytes) -> uuid.UUID:  # noqa: F811
    sha = await blobs.put(data)
    att_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        s.add(
            Attachment(
                id=att_id,
                note_id=uuid.UUID(note_id),
                domain_code="health",
                sha256=sha,
                filename="labs.pdf",
                media_type="application/pdf",
                size_bytes=len(data),
            )
        )
    return att_id


async def test_emr_parse_job_mints_facts_citing_real_page_chunks(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    att_id = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    # Ingest chunks the attached PDF: each page -> a text-layer chunk anchored "page N".
    await ingest(maker, note_id, tmp_path)

    await EmrImportPipeline(maker, blobs, _pipeline(maker)).parse({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        # The athena panel minted its ambulatory encounter + lab observations.
        obs = (
            (await s.execute(select(Entity).where(Entity.canonical_name == "Platelet count")))
            .scalars()
            .all()
        )
        assert obs and all(e.domain_code == "health" for e in obs)
        enc = (
            (await s.execute(select(Entity).where(func.lower(Entity.kind) == "encounter")))
            .scalars()
            .all()
        )
        assert enc

        # The lab_results projection is populated for the platelet reading.
        plt = (
            await s.execute(
                text("SELECT value_num FROM app.lab_results WHERE analyte = 'Platelet count'")
            )
        ).all()
        assert plt and plt[0][0] == 188.0

        # Every value fact cites a REAL chunk that belongs to the PDF attachment's page,
        # not a stub — the citation the arbiter froze points at the page it was printed on.
        value_facts = (
            (
                await s.execute(
                    select(Fact).where(
                        Fact.note_id == uuid.UUID(note_id),
                        Fact.predicate == "value",
                        Fact.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert value_facts
        cited_chunk_ids = {f.chunk_id for f in value_facts if f.chunk_id is not None}
        assert cited_chunk_ids
        att_chunk_ids = {
            r[0]
            for r in (
                await s.execute(
                    select(Chunk.id).where(
                        Chunk.attachment_id == att_id, Chunk.source_kind == "text-layer"
                    )
                )
            ).all()
        }
        assert cited_chunk_ids <= att_chunk_ids  # every citation is a real page chunk


async def test_emr_parse_job_is_idempotent(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    await ingest(maker, note_id, tmp_path)
    handler = EmrImportPipeline(maker, blobs, _pipeline(maker))
    await handler.parse({"note_id": note_id})
    await handler.parse({"note_id": note_id})  # a safe re-run: no crash, projection stays correct
    async with scoped_session(maker, SYSTEM_CTX) as s:
        # The projection is idempotent: exactly one current platelet row and one
        # encounter FOR THIS NOTE, regardless of the re-run's retract-and-re-mint sweep.
        plt = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.lab_results"
                    " WHERE analyte = 'Platelet count' AND source_note_id = :n AND is_current"
                ),
                {"n": note_id},
            )
        ).scalar_one()
        assert plt == 1
        encs = (
            await s.execute(
                text("SELECT count(*) FROM app.encounters WHERE source_note_id = :n"),
                {"n": note_id},
            )
        ).scalar_one()
        assert encs == 1
