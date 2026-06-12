"""Agent ORM models. `AgentSession` is the capability record: which domains and
subjects a session may read (docs/ASSISTANT.md "Session capabilities")."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    # Selected read scope: domain codes and subject ids the session may read.
    domain_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    subject_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentRun(Base):
    """One turn-loop execution — the audit/training trace (owner-only)."""

    __tablename__ = "agent_runs"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id")
    )
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    prompt_version: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentStep(Base):
    """One step within a run: a model turn or a tool call."""

    __tablename__ = "agent_steps"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.agent_runs.id"))
    idx: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    tool_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    cost_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
