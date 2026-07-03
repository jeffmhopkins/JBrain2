"""EMR intake orchestration against real Postgres (docs/plans/EMR_IMPORT_PLAN.md
§6.1) — the delete-last / scrub-before-index / fail-closed security path. A real
AES-encrypted ZIP (built with pyzipper) carrying a real PDF (built with PyMuPDF)
is attached to a health note; the handler must normalize it in place on success
and destroy nothing + card on failure.
"""

import io
import uuid

import pymupdf
import pytest
import pyzipper
from sqlalchemy import select, text

from jbrain.db.session import scoped_session
from jbrain.ingest.emr.intake_handler import EmrIntakeError, EmrIntakePipeline
from jbrain.models.notes import Attachment, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Synthetic lab record")
    out = doc.tobytes()
    doc.close()
    return out


def _aes_zip(files: dict[str, bytes], password: str) -> bytes:
    buf = io.BytesIO()
    with pyzipper.AESZipFile(
        buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(password.encode())
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def _attach_zip(maker, blobs, note_id: str, zip_bytes: bytes) -> None:  # noqa: F811
    sha = await blobs.put(zip_bytes)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        s.add(
            Attachment(
                note_id=uuid.UUID(note_id),
                domain_code="health",
                sha256=sha,
                filename="records.zip",
                media_type="application/zip",
                size_bytes=len(zip_bytes),
            )
        )


async def _attachments(maker, note_id: str) -> list[Attachment]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return list(
            (await s.execute(select(Attachment).where(Attachment.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )


async def _body(maker, note_id: str) -> str:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(select(Note.body).where(Note.id == uuid.UUID(note_id)))
        ).scalar_one()


async def test_intake_normalizes_the_note_in_place(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path / "blobs")
    note_id = await make_note(
        maker, domain="health", body="here are my health records, password: hunter2"
    )
    await _attach_zip(maker, blobs, note_id, _aes_zip({"lab.pdf": _pdf()}, "hunter2"))

    enqueued: list[str] = []

    async def _enqueue(nid: str) -> None:
        enqueued.append(nid)

    await EmrIntakePipeline(maker, blobs, _enqueue).intake({"note_id": note_id})

    media = {a.media_type for a in await _attachments(maker, note_id)}
    assert "application/pdf" in media  # the decrypted PDF is attached
    assert "application/zip" not in media  # the zip is deleted (delete-last)
    body = await _body(maker, note_id)
    assert "hunter2" not in body and "[redacted]" in body  # secret scrubbed
    assert enqueued == [note_id]  # re-ingest enqueued to chunk the scrubbed note


async def test_intake_is_idempotent_on_re_run(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path / "blobs")
    note_id = await make_note(maker, domain="health", body="records, password: pw123")
    await _attach_zip(maker, blobs, note_id, _aes_zip({"lab.pdf": _pdf()}, "pw123"))
    pipe = EmrIntakePipeline(maker, blobs)
    await pipe.intake({"note_id": note_id})
    # A second run finds no zip -> no-op; the PDF attachment is not duplicated.
    await pipe.intake({"note_id": note_id})
    media = [a.media_type for a in await _attachments(maker, note_id)]
    assert media.count("application/pdf") == 1


async def test_intake_fails_closed_on_wrong_password(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path / "blobs")
    note_id = await make_note(maker, domain="health", body="records, password: guessed")
    await _attach_zip(maker, blobs, note_id, _aes_zip({"lab.pdf": _pdf()}, "actual-secret"))

    with pytest.raises(EmrIntakeError):
        await EmrIntakePipeline(maker, blobs).intake({"note_id": note_id})

    # The only copy is preserved: the zip stays, no PDF, body unchanged.
    media = {a.media_type for a in await _attachments(maker, note_id)}
    assert media == {"application/zip"}
    assert await _body(maker, note_id) == "records, password: guessed"
    async with scoped_session(maker, SYSTEM_CTX) as s:
        cards = (
            await s.execute(
                text(
                    "SELECT payload FROM app.review_items"
                    " WHERE kind = 'low_confidence' AND payload->>'note_id' = :nid"
                ),
                {"nid": note_id},
            )
        ).all()
    assert any((p or {}).get("subkind") == "emr_intake_failed" for (p,) in cards)


async def test_intake_fails_closed_when_no_password(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path / "blobs")
    note_id = await make_note(maker, domain="health", body="records attached (forgot the password)")
    await _attach_zip(maker, blobs, note_id, _aes_zip({"lab.pdf": _pdf()}, "secret"))
    with pytest.raises(EmrIntakeError):
        await EmrIntakePipeline(maker, blobs).intake({"note_id": note_id})
    media = {a.media_type for a in await _attachments(maker, note_id)}
    assert media == {"application/zip"}  # nothing destroyed
