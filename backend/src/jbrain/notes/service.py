"""Note capture and retrieval over an abstract repository (same pattern as auth)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from jbrain.db.session import SessionContext


class UnknownDomain(Exception):
    pass


class ClarificationsAltered(Exception):
    """A PATCH body did not end in the note's own clarification blocks (D6).

    The editor is served the COMPOSED text, so a save it did not mangle always ends in
    exactly what `compose_body` appended. When it does not, the repo has no safe reading:
    treating the whole string as the body doubles the blocks on the next compose, and
    cutting at the marker deletes whatever the owner wrote after a paragraph that merely
    looks like one. So the write is refused and the note left untouched — the API answers
    409 and the owner's text is still in the editor.
    """


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
    # Whether a non-empty description was cached (full image analysis) —
    # flips the chip to "text + description".
    has_description: bool = False


@dataclass(frozen=True)
class ExtractInfo:
    """One vision-cache row, as the manifest expansion fetches it."""

    kind: str
    text: str
    tool: str
    confidence: float | None
    created_at: datetime
    # Per-word transcript breakdown (transcript rows only; None otherwise).
    words: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class NoteInfo:
    id: str
    client_id: str
    domain: str
    destination: str | None
    # The note's TEXT: the frozen author body plus any appended clarification blocks
    # (D6, jbrain.notes.compose). A PATCH of this string is stripped back to the body
    # before it is stored, so an untouched edit round trip is a no-op.
    body: str
    created_at: datetime
    # Client capture-time UTC offset (minutes east of UTC); None for
    # server-stamped or pre-Phase-3 rows. The extraction anchor uses it to
    # recover the note's local date.
    tz_offset_minutes: int | None = None
    # 'pending' | 'processing' | 'indexed' | 'failed' — drives indexing chips.
    ingest_state: str = "pending"
    # True once hidden from the home stream (still searchable; see set_hidden).
    hidden: bool = False
    # True once note.extract has written the note_analysis row — the quiet
    # end of the pipeline lifecycle chip (indexing → ocr → analyzing → gone).
    analyzed: bool = False
    # 'human' (captured by the owner) or 'agent' (enacted from a Proposal). The
    # attribution rides as metadata, not in the body, so the citable source text
    # stays clean (docs/reference/ASSISTANT.md #7).
    provenance: str = "human"
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
        attachments_expected: int = 0,
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

        None when the note doesn't exist or is invisible under RLS; raises
        UnknownDomain for a bogus domain move, and ClarificationsAltered when the
        body sent back is not the note's composed text with its D6 blocks intact.
        """
        ...

    async def append_clarification(
        self,
        ctx: SessionContext,
        note_id: str,
        *,
        question: str,
        answer: str,
        session_id: str | None = None,
    ) -> NoteInfo | None:
        """Append one clarification block to the note and re-drive ingestion (D6).

        The body column is untouched — the block is a row, composed onto the note's
        text at read time — so an owner body edit cannot destroy it and it cannot
        shift the body's character offsets. Re-ingest is enqueued in the same
        transaction, because the note's text (and therefore its chunks, and the graph
        derived from them) just changed.

        Returns the note with the new block already composed in; None when the note
        doesn't exist, is deleted, or is outside ctx's domain scopes. There is no HTTP
        route for this: W3's `ask_owner` tool is the caller.
        """
        ...

    async def delete_note(self, ctx: SessionContext, note_id: str) -> bool:
        """Soft-delete the note and hard-delete its chunks (search hygiene)."""
        ...

    async def set_hidden(self, ctx: SessionContext, note_id: str, hidden: bool) -> bool:
        """Toggle the note's home-stream visibility. Chunks are left intact so
        a hidden note stays searchable. False when missing or out of scope."""
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

    async def list_extracts(
        self, ctx: SessionContext, attachment_id: str
    ) -> list[ExtractInfo] | None:
        """The attachment's vision-cache rows (may be empty); None when the
        attachment is missing or out of scope."""
        ...

    async def remove_attachment(self, ctx: SessionContext, attachment_id: str) -> str | None:
        """Deletes the row (never the shared blob); returns the note_id for
        re-ingestion, or None when missing/out of scope."""
        ...
