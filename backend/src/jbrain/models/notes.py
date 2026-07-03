import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from jbrain.models.analysis import NoteAnalysis
from jbrain.models.core import Base

# The note→graph Integrator lifecycle (docs/archive/INTEGRATOR_PLAN.md §4). Mirrored in
# migration 0029's CHECK constraint — keep the two in sync.
INTEGRATION_STATES = frozenset(
    {"pending_integration", "integrating", "integrated", "stale", "skipped"}
)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(Text, unique=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    destination: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text)
    # 'indexed' means chunked + FTS-searchable; embeddings arrive in Step 3.
    ingest_state: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The note→graph Integrator lifecycle (INTEGRATION_STATES). An indexed note
    # is 'pending_integration' until the integrate_note job runs and commits it.
    integration_state: Mapped[str] = mapped_column(
        Text, default="pending_integration", server_default="pending_integration"
    )
    # Phase-6 wiki dirty bit (mark-and-sweep): false at create/edit, set true once a wiki
    # build has incorporated the note. The builder targets wiki_built = false notes.
    wiki_built: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Capture location: owner-eyes metadata, excluded from Phase 7 scoped views.
    latitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Double, nullable=True)
    location_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Client's capture-time UTC offset in minutes east of UTC; lets the
    # extraction anchor be the note's LOCAL date even though created_at
    # round-trips through timestamptz as a UTC instant.
    tz_offset_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hidden from the entry-mode home stream but still a source of truth:
    # chunks/embeddings are untouched, so the note stays searchable. NULL =
    # visible; an instant = when it was hidden. Distinct from deleted_at.
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 'human' (the default for captured notes), 'agent' (an agent-authored note enacted from a
    # Proposal — NORMAL extraction weight), or 'owner_correction' (an owner correction note,
    # Phase 6 §4: full-weight, force-supersedes + pins). source_ref attributes it to what
    # prompted it (the proposal/conversation id).
    provenance: Mapped[str] = mapped_column(Text, default="human", server_default="human")
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The wiki revision an owner correction note disputes (migration 0051); nullable.
    wiki_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_revisions.id", ondelete="SET NULL"), nullable=True
    )
    # Whether note.extract has produced this note's note_analysis row — the
    # API's "analysis done" signal for the lifecycle chip. A correlated EXISTS
    # (the has_extracts pattern) so list/get serialization needs no second
    # query; the row only appears when the integrate_note job commits.
    analyzed: Mapped[bool] = column_property(
        select(NoteAnalysis.note_id).where(NoteAnalysis.note_id == id).exists()
    )

    attachments: Mapped[list["Attachment"]] = relationship(lazy="selectin")


class AttachmentExtract(Base):
    """One vision-backend product for an attachment (migration 0010): OCR and
    caption are separate products (docs/reference/ANALYSIS.md "Attachments"). Ingest
    reads these as a pure cache; only the ocr_attachment job writes them, and
    re-OCR is delete + insert (the chunks pattern)."""

    __tablename__ = "attachment_extracts"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.attachments.id")
    )
    kind: Mapped[str] = mapped_column(Text)  # 'ocr' | 'caption' | 'transcript' | 'video_analysis'
    tool: Mapped[str] = mapped_column(Text)  # provider:model provenance
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-word transcript breakdown (migration 0081), transcript rows only:
    # [{"text", "start_ms", "end_ms", "confidence"}, ...] for the karaoke UI.
    # Display-only; the searchable chunks use `text`. NULL for ocr/caption.
    words: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Structured analyze_video result (migration 0083), video_analysis rows only:
    # {"duration_ms", "frames": [{"t_ms", "caption", "thumb_id"}, ...],
    #  "transcript": {"text", "words", "duration_ms"} | None}. `thumb_id` is a blob
    # sha256 (no URLs — invariant #9). Display-only; the summary in `text` chunks.
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.notes.id"))
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    sha256: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Whether any vision extract exists — the API's OCR-status signal. A
    # correlated EXISTS so list/get serialization needs no second query.
    has_extracts: Mapped[bool] = column_property(
        select(AttachmentExtract.id).where(AttachmentExtract.attachment_id == id).exists()
    )
    # Whether a non-empty description (kind 'caption') exists — drives the
    # "text + description" chip without the client fetching extracts.
    has_description: Mapped[bool] = column_property(
        select(AttachmentExtract.id)
        .where(
            AttachmentExtract.attachment_id == id,
            AttachmentExtract.kind == "caption",
            AttachmentExtract.text != "",
        )
        .exists()
    )


class Chunk(Base):
    """Searchable slice of a note or attachment segment.

    The `tsv` (DB-generated) and `embedding` (pgvector, filled by Step 3 via
    SQL) columns are deliberately unmapped: the ORM never writes them, and
    mapping `embedding` would require a pgvector SQLAlchemy type this phase
    doesn't need.
    """

    __tablename__ = "chunks"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.notes.id"))
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.attachments.id"), nullable=True
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    granularity: Mapped[str] = mapped_column(Text)  # 'paragraph' | 'section'
    seq: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kind: Mapped[str] = mapped_column(Text, default="note", server_default="note")
    source_anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
