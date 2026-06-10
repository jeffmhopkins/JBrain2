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


@dataclass(frozen=True)
class NoteInfo:
    id: str
    client_id: str
    domain: str
    destination: str | None
    body: str
    created_at: datetime
    attachments: list[AttachmentInfo] = field(default_factory=list)


class NotesRepo(Protocol):
    async def create_note(
        self,
        ctx: SessionContext,
        *,
        client_id: str,
        domain: str,
        destination: str | None,
        body: str,
    ) -> tuple[NoteInfo, bool]:
        """Insert or return the existing note for client_id; bool = created."""
        ...

    async def list_notes(
        self, ctx: SessionContext, *, limit: int, before: datetime | None
    ) -> list[NoteInfo]:
        """Newest first, optionally strictly older than `before`."""
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
