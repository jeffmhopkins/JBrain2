"""Workflow-engine ORM models (docs/archive/WORKFLOW_ENGINE_PLAN.md §3).

The data-defined engine substrate created by migration 0036: the event log, the
trigger/pipeline/schedule definitions, and the persisted resolution pins.
`runs`/`run_steps` live in `models.agent` (`Run`/`RunStep`) — they are the in-place
`agent_runs` rename from migration 0037 — and `actions` is the sibling W0.1 registry
task.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class Pipeline(Base):
    """A stored pipeline definition: an ordered set of action refs (E3). Global
    reference data (canonical_predicates precedent) — a narrowed reader resolves
    an action ref, only the owner/system context revises. name+version is the
    address; a change is a new version, never an edit."""

    __tablename__ = "pipelines"
    __table_args__ = {"schema": "app"}

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, server_default="1")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Schedule(Base):
    """A scheduler claim target: an explicit next_run_at advanced app-side so a fake
    clock controls it (N3). Owner/system config.

    `schedule_kind` selects how the next fire is computed: `interval` is the legacy
    fixed forward step (the reconcilers' sub-day cadences); `on_demand` / `once` /
    `repeat` mirror `app.tasks` (migration 0093) so a sweep can be scheduled on a
    wall-clock day/time, reusing `jbrain.tasks.schedule.next_run_after`."""

    __tablename__ = "schedules"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Only set for the 'interval' kind; a spec-driven schedule leaves it NULL.
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    schedule_kind: Mapped[str] = mapped_column(Text, default="interval", server_default="interval")
    schedule_freq: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, server_default="{}"
    )
    schedule_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL when the schedule has no upcoming fire (on_demand, or a spent once).
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Trigger(Base):
    """Binds an event type (on_event) OR a schedule (on_schedule_id) to a pipeline;
    exactly one source. manual=true marks an emergency-fireable sweep. Owner
    config."""

    __tablename__ = "triggers"
    __table_args__ = (
        CheckConstraint(
            "(on_event IS NULL) <> (on_schedule_id IS NULL)",
            name="triggers_one_source",
        ),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    on_event: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.schedules.id", ondelete="CASCADE"), nullable=True
    )
    pipeline: Mapped[str] = mapped_column(Text)
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Event(Base):
    """An append-only event-log row. domain_code is the fail-closed stamp (E2);
    principal_id is the triggering identity (E1). dispatched_at NULL until the
    dispatcher has fanned it out. Domain-firewalled."""

    __tablename__ = "events"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResolutionPin(Base):
    """Persists the pure analysis.pins.ResolutionPin. PK includes chunk_id because
    occurrence_index is chunk-relative (the pins.py A8 warning); cascades with the
    note (N15). Exactly one of entity_id / normalized_predicate per decision_kind.
    Domain-firewalled by the note's domain."""

    __tablename__ = "resolution_pin"
    __table_args__ = (
        PrimaryKeyConstraint(
            "note_id",
            "chunk_id",
            "occurrence_index",
            "decision_kind",
            name="resolution_pin_pkey",
        ),
        CheckConstraint(
            "(decision_kind = 'identity'"
            " AND entity_id IS NOT NULL AND normalized_predicate IS NULL)"
            " OR (decision_kind = 'predicate_key'"
            " AND normalized_predicate IS NOT NULL AND entity_id IS NULL)",
            name="resolution_pin_one_decision",
        ),
        {"schema": "app"},
    )

    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="CASCADE")
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.chunks.id", ondelete="CASCADE")
    )
    occurrence_index: Mapped[int] = mapped_column(Integer)
    decision_kind: Mapped[str] = mapped_column(Text)  # identity | predicate_key
    surface: Mapped[str] = mapped_column(Text)
    span_text_hash: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.entities.id", ondelete="CASCADE"), nullable=True
    )
    normalized_predicate: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
