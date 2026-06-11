"""Note capture and retrieval over an abstract repository (same pattern as auth)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from jbrain.db.session import SessionContext


class UnknownDomain(Exception):
    pass


@dataclass(frozen=True)
class AttachmentInfo:
    id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str = ""
    # Whether any vision extract (OCR/caption) exists — drives the
    # Attachments tab's OCR status chip.
    has_extracts: bool = False


@dataclass(frozen=True)
class NoteInfo:
    id: str
    client_id: str
    domain: str
    destination: str | None
    body: str
    created_at: datetime
    # Client capture-time UTC offset (minutes east of UTC); None for
    # server-stamped or pre-Phase-3 rows. The extraction anchor uses it to
    # recover the note's local date.
    tz_offset_minutes: int | None = None
    # 'pending' | 'processing' | 'indexed' | 'failed' — drives indexing chips.
    ingest_state: str = "pending"
    attachments: list[AttachmentInfo] = field(default_factory=list)
    # Capture location: owner-eyes metadata (Phase 7 scoped views exclude it).
    latitude: float | None = None
    longitude: float | None = None
    accuracy_m: float | None = None


@dataclass(frozen=True)
class NoteUpdate:
    """PATCH semantics: None means leave unchanged; destination needs the
    explicit clear flag because null is also its 'unset' value."""

    body: str | None = None
    domain: str | None = None
    destination: str | None = None
    clear_destination: bool = False


class NotesRepo(Protocol):
    async def create_note(
        self,
        ctx: SessionContext,
        *,
        client_id: str,
        domain: str,
        destination: str | None,
        body: str,
        created_at: datetime | None = None,
        tz_offset_minutes: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
    ) -> tuple[NoteInfo, bool]:
        """Insert or return the existing note for client_id; bool = created."""
        ...

    async def list_notes(
        self, ctx: SessionContext, *, limit: int, before: datetime | None
    ) -> list[NoteInfo]:
        """Newest first, optionally strictly older than `before`."""
        ...

    async def get_note(self, ctx: SessionContext, note_id: str) -> NoteInfo | None:
        """None when missing, deleted, or outside ctx's domain scopes."""
        ...

    async def update_note(
        self, ctx: SessionContext, note_id: str, changes: NoteUpdate
    ) -> NoteInfo | None:
        """Apply changes, stamp updated_at, reset ingest_state to 'pending'.

        None when the note doesn't exist or is invisible under RLS;
        raises UnknownDomain for a bogus domain move.
        """
        ...

    async def delete_note(self, ctx: SessionContext, note_id: str) -> bool:
        """Soft-delete the note and hard-delete its chunks (search hygiene)."""
        ...

    async def add_attachment(
        self,
        ctx: SessionContext,
        *,
        note_id: str,
        sha256: str,
        filename: str,
        media_type: str,
        size_bytes: int,
    ) -> AttachmentInfo | None:
        """None when the note doesn't exist or is outside ctx's domain scopes."""
        ...

    async def get_attachment(
        self, ctx: SessionContext, attachment_id: str
    ) -> AttachmentInfo | None: ...

    async def remove_attachment(self, ctx: SessionContext, attachment_id: str) -> str | None:
        """Deletes the row (never the shared blob); returns the note_id for
        re-ingestion, or None when missing/out of scope."""
        ...
