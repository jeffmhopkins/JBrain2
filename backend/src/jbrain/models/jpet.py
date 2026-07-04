"""JPet ORM model (docs/plans/JPET_PLAN.md). `pet_state` is the server-authoritative
wall-pet row — owner-only, single-domain, one per (principal, domain). The Wall and
the phone Control screen both render this row; a drives tick and `/pet/command`
mutate it. Mirrors migration 0123 (the migration is the source of truth)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class PetMemory(Base):
    """One episodic memory — a child's message, a care event — the pet recalls. The
    most recent are woven back into the `pet.turn` prompt (docs/plans/JPET_PLAN.md W5).
    Owner-only, single-domain (mirror of `pet_state`; migration 0124)."""

    __tablename__ = "pet_memory"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    kind: Mapped[str] = mapped_column(Text, default="said", server_default="said")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PetState(Base):
    """One pet, pinned to a single domain. v3 has no drive meters — the pet's continuous
    life is the wall's own sim. The server keeps durable state: `name`, `mood`/`emotion`
    (plain labels for the phone header), the current command `script` + room `objects`, and
    the `pos`/`target`/`facing`/`action` a command sets. Mirrors migrations 0123–0126."""

    __tablename__ = "pet_state"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    name: Mapped[str] = mapped_column(Text)

    mood: Mapped[str] = mapped_column(Text, default="neutral", server_default="neutral")
    emotion: Mapped[str] = mapped_column(Text, default="neutral", server_default="neutral")
    speech: Mapped[str | None] = mapped_column(Text, nullable=True)
    asleep: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    pos_x: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    pos_z: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    target_x: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    target_z: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    facing: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    action: Mapped[str] = mapped_column(Text, default="idle", server_default="idle")

    # v2 (migration 0125): the bounded action script the pet plays out, the room objects
    # it can target/carry (a fixed set of mutable {kind: [x, z]} positions), what it is
    # currently carrying, and the day/night light state.
    script: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    script_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    objects: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    carrying: Mapped[str | None] = mapped_column(Text, nullable=True)
    lights_on: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    last_tick_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
