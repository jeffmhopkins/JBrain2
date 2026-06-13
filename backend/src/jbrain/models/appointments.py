"""Appointment ORM model (schema/defs/types/appointment.yaml). An appointment is
a typed projection of one appointment entity — owner-managed, single-domain,
materialized by the projector from the citable graph (never written directly)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class Appointment(Base):
    """One appointment, projected from `entity_id`. `rrule` is an RFC-5545
    recurrence (NULL = single event); `status` follows the appointment.yaml
    Lifecycle enum (tentative/confirmed/cancelled/occurred). Owner-only and
    domain-scoped by RLS — no principal column (the graph it projects is
    domain-scoped, not principal-stamped)."""

    __tablename__ = "appointments"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.entities.id", ondelete="CASCADE"), unique=True
    )
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="confirmed", server_default="confirmed")
    rrule: Mapped[str | None] = mapped_column(Text, nullable=True)
    attendees: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    source_note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
