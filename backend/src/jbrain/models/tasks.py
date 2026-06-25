"""Task ORM models (docs/mocks/tasks-launcher-README.md).

A `Task` is a saved prompt that spawns an agent session on a schedule (recurring /
one-off) or on demand. A `TaskRun` records one execution and points at the
`agent_session` it produced, so the owner can open the historical session. Both
tables are owner-only metadata (RLS `is_owner()`, like `agent_sessions`); the
agent's *reads* during a run still go through the session's narrowed firewall.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class Task(Base):
    """A saved prompt + persona + schedule. `next_run_at` is the computed next fire
    (NULL for on-demand or a spent one-off); the scheduler claims rows whose
    `next_run_at <= now`."""

    __tablename__ = "tasks"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    name: Mapped[str] = mapped_column(Text, default="", server_default="")
    prompt: Mapped[str] = mapped_column(Text)
    # The persona the run executes as — its data access is the firewall, not a label
    # (a non-KB agent runs with empty read scopes). Constrained by a DB CHECK.
    agent: Mapped[str] = mapped_column(Text, default="jerv", server_default="jerv")
    # Selected read scope; only honoured for a knowledge-base agent (curator).
    domain_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    # Schedule spec: kind ∈ on_demand|once|repeat; for repeat, freq ∈
    # daily|weekdays|weekly with `days` (Sun=0..Sat=6) for weekly + `time` "HH:MM";
    # for once, `run_at` is the absolute fire instant. `timezone` interprets `time`.
    schedule_kind: Mapped[str] = mapped_column(
        Text, default="on_demand", server_default="on_demand"
    )
    schedule_freq: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_days: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), default=list, server_default="{}"
    )
    schedule_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Delivery (the run's result is always saved to history; these are the extras).
    notify_push: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    home_card: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskRun(Base):
    """One execution of a task. `session_id` points at the spawned agent session
    (ON DELETE SET NULL so deleting a chat doesn't break run history); the row
    cascades when its task is deleted."""

    __tablename__ = "task_runs"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.tasks.id", ondelete="CASCADE")
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    # How it fired — schedule | manual (a "Run now").
    trigger: Mapped[str] = mapped_column(Text, default="schedule", server_default="schedule")
    # A short, owner-only excerpt of the answer for the run-history row.
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
