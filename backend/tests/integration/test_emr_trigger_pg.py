"""The EMR-import trigger wiring end-to-end against real Postgres
(docs/plans/EMR_IMPORT_PLAN.md §6.0): ingest of a health `Records` note carrying an
archive emits a `note.ingested` event whose widened payload carries the
pre-decryption markers, and a live dispatcher tick resolves the seeded `emr_import`
trigger into exactly one intake job. A plain note trips neither stage.
"""

import json
import uuid

import pymupdf
import pytest
from sqlalchemy import text

from jbrain.db.session import scoped_session
from jbrain.ingest.emr.import_handler import EMR_PARSE_SPEC
from jbrain.ingest.emr.intake_handler import EMR_IMPORT_SPEC
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.workflow import dispatcher
from jbrain.workflow import events as wf_events
from jbrain.workflow.registry import ACTION_SPECS, build_registry
from jbrain.workflow.runlog import PipelineRunLog
from jbrain.workflow.scheduler import PURGE_ACTION
from tests.conftest import docker_available
from tests.integration.test_dispatcher_pg import (  # noqa: F401
    _seed_owner_principal,
    blobs,
    maker,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _registry():  # noqa: ANN202
    return build_registry((*ACTION_SPECS, PURGE_ACTION, EMR_IMPORT_SPEC, EMR_PARSE_SPEC))


async def _records_note(maker, *, body: str) -> str:  # noqa: F811
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"emr-{uuid.uuid4()}", domain="health", destination="Records", body=body
    )
    return note.id


async def _attach(maker, note_id: str, media_type: str, *, sha: str, size: int) -> None:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.attachments (id, note_id, domain_code, sha256, filename,"
                " media_type, size_bytes) VALUES (:id, :n, 'health', :sha, :fn, :mt, :sz)"
            ),
            {
                "id": str(uuid.uuid4()),
                "n": note_id,
                "sha": sha,
                "fn": "records.zip" if "zip" in media_type else "lab.pdf",
                "mt": media_type,
                "sz": size,
            },
        )


def _min_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Synthetic lab page")
    out = doc.tobytes()
    doc.close()
    return out


async def _ingested_payload(maker, note_id: str) -> dict:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        raw = (
            await s.execute(
                text(
                    "SELECT payload::text FROM app.events"
                    " WHERE type = :t AND payload->>'note_id' = :n"
                ),
                {"t": wf_events.NOTE_INGESTED, "n": note_id},
            )
        ).scalar_one()
    return json.loads(raw)


async def _count_jobs(maker, *, kind: str, note_id: str) -> int:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.jobs WHERE kind = :k AND payload->>'note_id' = :n"),
                {"k": kind, "n": note_id},
            )
        ).scalar_one()


async def test_archive_note_emits_markers_and_dispatches_intake(maker, blobs):  # noqa: F811
    await _seed_owner_principal(maker)
    note_id = await _records_note(maker, body="here are my records, password: hunter2")
    # A zip needs no blob: ingest has no zip extractor, so it never fetches it.
    await _attach(maker, note_id, "application/zip", sha=uuid.uuid4().hex, size=10)

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    # The widened note.ingested payload carries the pre-decryption markers (§12.2 #4).
    payload = await _ingested_payload(maker, note_id)
    assert payload["destination"] == "Records"
    assert payload["has_zip_attachment"] is True
    assert payload["has_pdf_attachment"] is False

    # A live tick resolves the seeded emr_import trigger into exactly one intake job.
    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))
    assert await _count_jobs(maker, kind="emr_import", note_id=note_id) == 1
    # The parse stage does NOT fire yet — the archive is still encrypted.
    assert await _count_jobs(maker, kind="emr_parse", note_id=note_id) == 0


async def test_decrypted_note_dispatches_parse_not_intake(maker, blobs):  # noqa: F811
    # After intake the zip is gone and a PDF is attached: the SAME note.ingested markers
    # now select stage 2 (emr_parse), and stage 1 (emr_import) no longer matches.
    await _seed_owner_principal(maker)
    note_id = await _records_note(maker, body="records (decrypted)")
    pdf = _min_pdf()
    sha = await blobs.put(pdf)  # ingest extracts the PDF, so its blob must exist
    await _attach(maker, note_id, "application/pdf", sha=sha, size=len(pdf))

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    payload = await _ingested_payload(maker, note_id)
    assert payload["has_zip_attachment"] is False and payload["has_pdf_attachment"] is True

    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))
    assert await _count_jobs(maker, kind="emr_parse", note_id=note_id) == 1
    assert await _count_jobs(maker, kind="emr_import", note_id=note_id) == 0


async def test_plain_records_note_trips_neither_stage(maker, blobs):  # noqa: F811
    # A Medical/Records note with no attachment is an ordinary note — no EMR job.
    await _seed_owner_principal(maker)
    note_id = await _records_note(maker, body="just a clinic note, no attachments")

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})
    await dispatcher.dispatcher_tick(maker, _registry(), live=True, run_log=PipelineRunLog(maker))

    assert await _count_jobs(maker, kind="emr_import", note_id=note_id) == 0
    assert await _count_jobs(maker, kind="emr_parse", note_id=note_id) == 0
