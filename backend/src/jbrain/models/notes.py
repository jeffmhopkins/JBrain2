import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from jbrain.models.core import Base


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

    attachments: Mapped[list["Attachment"]] = relationship(lazy="selectin")


class AttachmentExtract(Base):
    """One vision-backend product for an attachment (migration 0010): OCR and
    caption are separate products (docs/ANALYSIS.md "Attachments"). Ingest
    reads these as a pure cache; only the ocr_attachment job writes them, and
    re-OCR is delete + insert (the chunks pattern)."""

    __tablename__ = "attachment_extracts"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.attachments.id")
    )
    kind: Mapped[str] = mapped_column(Text)  # 'ocr' | 'caption'
    tool: Mapped[str] = mapped_column(Text)  # provider:model provenance
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
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
