"""EMR intake orchestration (docs/plans/EMR_IMPORT_PLAN.md §6.1): normalize a
health note carrying an encrypted-ZIP attachment in place — decrypt, attach the
raw PDFs to the SAME note, then DELETE the zip + SCRUB the password from the body,
all delete-last / fail-closed. On any failure before the delete-last step the zip
and original body are kept, no facts are written, and a review card is filed —
an intake must never destroy the only copy on a failed run.

The password lives ONLY in this handler's memory for the decrypt step (§6.1): it
is never written to a chunk, embedding, log, or setting. The scrub is persisted
BEFORE `ingest_note` runs (which snapshots the body and chunks/embeds it), so the
raw secret can never reach the index.
"""

from __future__ import annotations

from typing import Any

import pymupdf
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.ingest.emr.intake import ArchiveGuardError, extract_passwords, safe_extract
from jbrain.models.analysis import ReviewItem
from jbrain.models.notes import Attachment, Note
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import BlobStore

ZIP_MEDIA_TYPES = ("application/zip", "application/x-zip-compressed")
PDF_MEDIA_TYPE = "application/pdf"
REVIEW_KIND = "low_confidence"
FAIL_SUBKIND = "emr_intake_failed"
_REDACTED = "[redacted]"


class EmrIntakeError(Exception):
    """A fail-closed intake outcome (already carded); re-raised so the job records it."""


class EmrIntakePipeline:
    """The `emr_import` intake job handler. Constructor-injected deps mirror the
    shipped pipelines (OcrPipeline); `enqueue_ingest` re-chunks the normalized note."""

    def __init__(
        self,
        maker: async_sessionmaker,
        blobs: BlobStore,
        enqueue_ingest: Any = None,
    ) -> None:
        self._maker = maker
        self._blobs = blobs
        self._enqueue_ingest = enqueue_ingest

    async def intake(self, payload: dict[str, Any]) -> None:
        note_id = str(payload["note_id"])
        domain, err = await self._normalize(note_id)
        if err is not None:
            # Fail closed in a SEPARATE committing transaction (the normalize txn was
            # rolled back, discarding any partial attach), so the card actually persists.
            await self._file_card(note_id, domain or "health", str(err))
            raise EmrIntakeError(f"emr intake failed for note {note_id}: {err}")
        if domain is not None and self._enqueue_ingest is not None:
            await self._enqueue_ingest(note_id)

    async def _normalize(self, note_id: str) -> tuple[str | None, Exception | None]:
        """Decrypt + attach + delete-last/scrub in one transaction. Returns
        `(domain, None)` on success (enqueue re-ingest), `(None, None)` to skip (no
        zip / no note), or `(domain, error)` to fail closed. On failure the txn is
        rolled back so no partial attachment or half-scrub is ever committed."""
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            note = (
                await session.execute(select(Note).where(Note.id == note_id))
            ).scalar_one_or_none()
            if note is None:
                return None, None
            zip_att = (
                (
                    await session.execute(
                        select(Attachment).where(
                            Attachment.note_id == note_id,
                            Attachment.media_type.in_(ZIP_MEDIA_TYPES),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if zip_att is None:
                return None, None  # not an EMR-import note; nothing to do
            domain = note.domain_code
            body = note.body or ""
            passwords = extract_passwords(body)
            try:
                extracted = await self._decrypt(zip_att.sha256, passwords)  # in-memory only
            except Exception as exc:  # noqa: BLE001 — no DB write yet; fail closed
                return domain, exc
            try:
                for name, data in extracted:  # the first DB writes
                    await self._attach_pdf(session, note, name, data)
            except Exception as exc:  # noqa: BLE001 — discard partial attaches
                await session.rollback()
                return domain, exc

            # Delete-last: only now remove the zip and scrub the secret. The scrub is
            # persisted before the re-ingest that chunks/embeds the body.
            await session.execute(delete(Attachment).where(Attachment.id == zip_att.id))
            scrubbed = body
            for pw in sorted(set(passwords), key=len, reverse=True):
                scrubbed = scrubbed.replace(pw, _REDACTED)
            await session.execute(
                update(Note).where(Note.id == note_id).values(body=scrubbed, ingest_state="pending")
            )
            return domain, None

    async def _file_card(self, note_id: str, domain: str, reason: str) -> None:
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            await self._card(session, note_id, domain, reason)

    async def _decrypt(self, sha256: str, passwords: list[str]) -> list[tuple[str, bytes]]:
        zip_bytes = await self._blobs.get(sha256)
        if not passwords:
            raise EmrIntakeError("no decrypt password found in the note body")
        last: Exception | None = None
        for pw in passwords:
            try:
                members = safe_extract(zip_bytes, pw)
            except ArchiveGuardError:
                raise  # a hostile archive is fatal — don't try more passwords
            except Exception as exc:  # noqa: BLE001 — pyzipper's wrong-password error
                last = exc
                continue
            return [(m.filename, _decrypt_pdf(m.data, pw)) for m in members]
        raise EmrIntakeError(f"no candidate password decrypted the archive ({last})")

    async def _attach_pdf(self, session: Any, note: Note, name: str, data: bytes) -> None:
        sha = await self._blobs.put(data)
        # Idempotent: a re-run re-derives the same sha; don't duplicate the row.
        exists = (
            await session.execute(
                select(Attachment.id).where(Attachment.note_id == note.id, Attachment.sha256 == sha)
            )
        ).first()
        if exists is not None:
            return
        session.add(
            Attachment(
                note_id=note.id,
                domain_code=note.domain_code,
                sha256=sha,
                filename=name.rsplit("/", 1)[-1] or "record.pdf",
                media_type=PDF_MEDIA_TYPE,
                size_bytes=len(data),
            )
        )
        await session.flush()

    async def _card(self, session: Any, note_id: str, domain: str, reason: str) -> None:
        # One open card per note (retired on a successful re-run's re-ingest).
        existing = (
            await session.execute(
                text(
                    "SELECT 1 FROM app.review_items WHERE kind = :k AND status = 'open'"
                    " AND payload->>'note_id' = :nid AND payload->>'subkind' = :sk LIMIT 1"
                ),
                {"k": REVIEW_KIND, "nid": note_id, "sk": FAIL_SUBKIND},
            )
        ).first()
        if existing is not None:
            return
        session.add(
            ReviewItem(
                kind=REVIEW_KIND,
                payload={"note_id": note_id, "subkind": FAIL_SUBKIND, "reason": reason},
                domain_code=domain,
            )
        )
        await session.flush()


def _decrypt_pdf(data: bytes, password: str) -> bytes:
    """Return decrypted PDF bytes. If the member is a password-protected PDF, open
    and authenticate it (§6.1); a non-PDF or unencrypted member passes through
    unchanged. A wrong PDF password raises (the run fails closed)."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001 — not a PDF (or unreadable); keep the raw bytes
        return data
    try:
        if doc.needs_pass:
            if not doc.authenticate(password):
                raise EmrIntakeError("wrong password for an encrypted PDF member")
            return doc.tobytes()
        return data
    finally:
        doc.close()
