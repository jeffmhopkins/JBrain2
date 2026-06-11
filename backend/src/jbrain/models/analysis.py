"""Phase 3 analysis tables: entities, facts, temporal tokens, review inbox.

Mirrors migration 0006 (docs/ANALYSIS.md is the binding spec). Facts and
entities are append-mostly: supersession chains and merge tombstones are the
revision history, so nothing here is ever hard-deleted by application code —
with one sanctioned exception [decided]: deleting a source note purges every
artifact derived from it (jbrain.analysis.purge, grants in 0009), because
notes are the sole sources of truth and deletion is a privacy promise.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class Entity(Base):
    """A node in the property graph; properties live in facts.

    `summary_embedding` (pgvector, written via SQL like chunks.embedding) is
    deliberately unmapped — the ORM never touches it.
    """

    __tablename__ = "entities"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text)  # schema.org-guided, free text
    canonical_name: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-null when the entity is also a security subject (Mom the entity IS
    # Mom the subject) — attribution across subjects is a leak.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.subjects.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, default="provisional", server_default="provisional")
    # Merge tombstone: the entity row survives so the merge is reversible.
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.entities.id"), nullable=True
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.entities.id"))
    alias: Mapped[str] = mapped_column(Text)
    # Lowercased + dediacritized by the repo layer; see 0006 for why this is
    # not a DB-generated column.
    alias_norm: Mapped[str] = mapped_column(Text)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntityMention(Base):
    """Span-anchored entity link — what makes merges reversible."""

    __tablename__ = "entity_mentions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.entities.id"))
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.chunks.id"))
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.notes.id"))
    surface_text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    link_method: Mapped[str] = mapped_column(Text)  # exact_alias|embedding|llm|human
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EntityDistinction(Base):
    """Permanent distinct_from edge: a rejected merge, never re-proposed."""

    __tablename__ = "entity_distinctions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Canonical ordering (entity_a < entity_b) is enforced by the DB; callers
    # must sort the pair before insert.
    entity_a: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.entities.id"))
    entity_b: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.entities.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TemporalToken(Base):
    """A resolved date/time expression, span-anchored like a mention."""

    __tablename__ = "temporal_tokens"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.notes.id"))
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.chunks.id"), nullable=True
    )
    surface_phrase: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # point|range|recurrence
    resolved_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    temporal_precision: Mapped[str] = mapped_column(Text)  # instant|day|month|year|era|unknown
    # The anchor the phrase was resolved against — re-resolution after an
    # anchor correction is a targeted update keyed on this.
    capture_anchor: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rrule: Mapped[str | None] = mapped_column(Text, nullable=True)  # iCal RRULE for recurrences
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Fact(Base):
    """An edge in the property graph: entity.predicate[.qualifier] → value.

    The structural identity key (subject_id, entity_id, predicate, qualifier)
    is the graph address; the superseded_by chain on that address is the
    property's full revision history.
    """

    __tablename__ = "facts"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.subjects.id"), nullable=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.entities.id"))
    predicate: Mapped[str] = mapped_column(Text)  # schema.org-guided, free text
    qualifier: Mapped[str] = mapped_column(Text, default="", server_default="")
    kind: Mapped[str] = mapped_column(Text)  # event|measurement|state|attribute|preference|...
    statement: Mapped[str] = mapped_column(Text)
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.entities.id"), nullable=True
    )
    assertion: Mapped[str] = mapped_column(Text)  # asserted|negated|hypothetical|...
    # Bi-temporal: valid_* = true in the world; reported_at = client capture
    # time. Supersession compares validity time, never capture time.
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    temporal_precision: Mapped[str] = mapped_column(
        Text, default="unknown", server_default="unknown"
    )
    temporal_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.temporal_tokens.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    # Pinned facts are human overrides: reprocessing and auto-supersession
    # may re-flag them, never flip them.
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.facts.id"), nullable=True
    )
    # NULL = primary, note-sourced fact; non-NULL = this row is the pipeline-
    # materialized inverse of that source fact, whose lifecycle it shadows.
    derived_from_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.facts.id", ondelete="CASCADE"), nullable=True
    )
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.notes.id"))
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.chunks.id"), nullable=True
    )
    extractor: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteAnalysis(Base):
    """Per-note product of the note.extract call; analyzed_at is the Phase 3
    minimal reprocessing watermark (docs/ANALYSIS.md "Reprocessing")."""

    __tablename__ = "note_analysis"
    __table_args__ = {"schema": "app"}

    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id"), primary_key=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    extractor: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))


class ReviewItem(Base):
    """Generic review-inbox item; payload holds the row references the
    resolution handlers read plus the precomputed display fields the review
    card renders (jbrain.analysis.display)."""

    __tablename__ = "review_items"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text)  # fact_conflict|merge_proposal|...
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(Text, default="open", server_default="open")
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
