"""List ORM models (docs/ARCHITECTURE.md "Lists"). A list is owner-managed,
single-domain, structured data the agent maintains directly — items inherit
their list's visibility and order by `position`."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jbrain.models.core import Base


class List(Base):
    """One owner list, pinned to a single domain. `archived_at` retires it from
    the open view without deleting (the owner can still read it)."""

    __tablename__ = "lists"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    title: Mapped[str] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list["ListItem"]] = relationship(
        order_by="ListItem.position, ListItem.created_at", cascade="all, delete-orphan"
    )


class ListItem(Base):
    """A line in a list. `checked_at` is NULL while open; `source_note_id` traces
    an item the agent lifted from a note (nullable — bare items are allowed)."""

    __tablename__ = "list_items"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.lists.id", ondelete="CASCADE")
    )
    body: Mapped[str] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
