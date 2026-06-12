"""Agent ORM models. `AgentSession` is the capability record: which domains and
subjects a session may read (docs/ASSISTANT.md "Session capabilities")."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Text, func
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


class AgentMemory(Base):
    """Working/behavioral memory as rows rendered as MD (docs/ASSISTANT.md
    "Memory model"). Owner-only, domain-narrowed; behavioral tiers are
    owner-confirmed-write only (invariant #3). Append-only revisions: a delta
    edit writes a new row and points the old one's `superseded_by` at it."""

    __tablename__ = "agent_memory"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    # Behavioral/core memory references the owner subject only; plain uuid (no FK),
    # matching agent_sessions.subject_ids.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    block_kind: Mapped[str] = mapped_column(Text)  # core | task | self_semantic
    body_md: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_memory.id"), nullable=True
    )
    source: Mapped[str] = mapped_column(
        Text, default="owner_confirmed", server_default="owner_confirmed"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentEpisode(Base):
    """A conversation/task trace — auto-appended, never citable. Scoped to the
    SET of domains the turn touched (`domain_scopes`); a multi-scope episode is
    visible only to a session holding all of them (invariant #4). The
    segregated-namespace `embedding` (its own table, filled via SQL like
    chunks.embedding) keeps an episode from ever matching as a citable chunk."""

    __tablename__ = "agent_episodes"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    domain_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    body: Mapped[str] = mapped_column(Text)
    importance: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentEpisodeRef(Base):
    """A pointer (note/fact/entity id) from an episode back into the cited graph —
    never a copy (invariant #2). Cascades with its episode; the note FK is the
    purge target when a note is deleted (invariant #11). Exactly one id is set."""

    __tablename__ = "agent_episode_refs"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_episodes.id", ondelete="CASCADE")
    )
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="CASCADE"), nullable=True
    )
    fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.facts.id", ondelete="CASCADE"), nullable=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.entities.id", ondelete="CASCADE"), nullable=True
    )
