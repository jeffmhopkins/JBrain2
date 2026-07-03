"""Agent ORM models. `AgentSession` is the capability record: which domains and
subjects a session may read (docs/reference/ASSISTANT.md "Session capabilities")."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    # The selected agent persona (docs/reference/ASSISTANT.md "Agent selection"): which
    # system prompt, tool allowlist, and knowledge-base access the session runs
    # under. Defaults to the Full Brain curator; constrained by a DB CHECK.
    agent: Mapped[str] = mapped_column(Text, default="curator", server_default="curator")
    # Sub-agent lineage (docs/archive/SUBAGENT_SPAWNING_PLAN.md, migration 0105). A root
    # chat has parent_session_id=NULL/depth=0; a spawned child points at its parent
    # and carries depth=parent.depth+1 (DB-CHECKed to 0..2 — the two-sub-agent-layer
    # cap is structural). `no_memory` is the sandbox flag the spawn helper sets so a
    # child turn is never episodically appended. Deleting a parent cascades to its
    # children (they are sub-state of the parent turn, never orphaned top-level rows).
    parent_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="CASCADE"), nullable=True
    )
    depth: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    no_memory: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The last completed turn's context fill (its fullest step's prompt + output) and
    # the window it ran against — persisted so reopening the chat restores the
    # context-usage meter at once (token counts aren't in the stored transcript). Null
    # until a turn reports usage; a pre-feature chat stays null and the meter waits for
    # the next turn rather than showing a wrong figure.
    context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Selected read scope: domain codes and subject ids the session may read.
    domain_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    subject_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Run(Base):
    """One workflow run — the audit/training trace (owner-only). Generalizes the
    former `agent_runs` (migration 0037): `kind` discriminates agent vs
    integration/pipeline runs. `session_id`/`prompt_version` are nullable for
    session-less engine runs but required for `kind='agent'` (DB CHECK), so the
    agent invariant survives the relaxation. `ran_as` records E1's scope choice
    (scoped vs owner-system) for the audit; `domain_code`/`principal_id` carry the
    triggering stamp + identity the dispatcher narrows from (filled by sibling
    task A3)."""

    __tablename__ = "runs"
    __table_args__ = (
        # The agent invariant the nullable relaxation must not erode: an agent run
        # still carries both its session and prompt version.
        CheckConstraint(
            "kind <> 'agent' OR (session_id IS NOT NULL AND prompt_version IS NOT NULL)",
            name="runs_agent_requires_session",
        ),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(Text, default="agent", server_default="agent")
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="CASCADE"), nullable=True
    )
    pipeline: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The spawning parent's run, for the sub-agent tree cost rollup (migration
    # 0105). NULL for a root run; SET NULL on parent deletion so a child's audit
    # row survives. The kind CHECK admits 'subagent' for these child runs.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="SET NULL"), nullable=True
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.triggers.id", ondelete="SET NULL"), nullable=True
    )
    ran_as: Mapped[str] = mapped_column(Text, default="scoped", server_default="scoped")
    domain_code: Mapped[str | None] = mapped_column(
        Text, ForeignKey("app.domains.code"), nullable=True
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A free-text "processed X of Y" line a long-running job updates as it works (the
    # Ops "Runs" screen polls it live); cleared when the run closes. Counts/phase only.
    progress_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunStep(Base):
    """One step within a run: a model turn, a tool call, or an enqueued action.
    Generalizes the former `agent_steps` (migration 0037). `job_id` is a nullable
    FK to the executor job the step enqueued, SET NULL on job age-out so a run-log
    read never breaks (N2)."""

    __tablename__ = "run_steps"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="CASCADE")
    )
    idx: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    tool_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The executor job this step enqueued. Plain uuid (no ORM FK): app.jobs is not
    # a mapped table (it's the queue.py raw-SQL substrate), so an ORM FK would fail
    # mapper resolution; the DB-level FK (ON DELETE SET NULL, N2) lives in the
    # migration instead.
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    cost_tokens: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # The step's captured structured-log trace (the Runs "full logs" review view): a
    # JSONB array of compact event dicts a job emitted, or NULL when it logged nothing.
    detail: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentTurn(Base):
    """One conversation turn in a session's transcript (docs/reference/ASSISTANT.md
    "Sessions"). Owner-only; `seq` is the total insertion order (a user turn is
    written just before its assistant turn). Assistant turns carry the tool steps
    + note sources for the "Worked" block; user turns carry an empty list."""

    __tablename__ = "agent_turns"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(Text)  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    # The model's reasoning trace for an assistant turn (gpt-oss/GLM); "" otherwise.
    reasoning: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TurnAttachment(Base):
    """A file attached to a chat turn (image/PDF/text). Linked to the SESSION at
    upload (pre-upload, reference-by-id); `turn_id` is bound when the user turn is
    recorded (Stage-2 Wave 2). A NEW table rather than reusing app.attachments,
    whose `note_id` is NOT NULL. `domain_code` is the firewall scope, computed from
    the session's scopes at upload (TurnAttachmentRepo.domain_for_session)."""

    __tablename__ = "turn_attachments"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="CASCADE")
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_turns.id", ondelete="SET NULL"), nullable=True
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    sha256: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Vision-cache flags mirroring note attachments; populated in Wave 2 when the
    # OCR/caption pipeline lands for chat files.
    has_extracts: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    has_description: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Cached analyze_video result (migration 0084): {summary, duration_ms,
    # frames:[{t_ms, caption, thumb_id}], transcript:{text, words}|null}. NULL until
    # jerv analyses the clip; the thumbnail endpoint validates a thumb_id against the
    # frame list here before serving the blob (the firewall, invariant #3).
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class AgentMemory(Base):
    """Working/behavioral memory as rows rendered as MD (docs/reference/ASSISTANT.md
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
        UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="SET NULL"), nullable=True
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
