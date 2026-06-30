"""Guided-intake share-link ORM (migrations 0107/0108).

Three owner-owned tables behind a non-owner read path (the per-session
`intake_link` principal). The RLS firewall is Postgres' — these models carry no
authority; every query runs on an already-scoped session (see
`jbrain.intake.repo`). See `docs/GUIDED_INTAKE_PLAN.md` §5/§6.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class IntakeLink(Base):
    """A mintable share link: its config, run/open caps, and show-once secret hash."""

    __tablename__ = "intake_links"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app.subjects.id"))
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    label: Mapped[str] = mapped_column(Text, default="")
    persona_brief: Mapped[str] = mapped_column(Text, default="")
    fields_brief: Mapped[str] = mapped_column(Text, default="")
    opening_blurb: Mapped[str] = mapped_column(Text, default="")
    max_runs: Mapped[int] = mapped_column(Integer)
    runs_used: Mapped[int] = mapped_column(Integer, default=0)
    max_opens: Mapped[int] = mapped_column(Integer)
    opens_used: Mapped[int] = mapped_column(Integer, default=0)
    bind_on_first: Mapped[bool] = mapped_column(Boolean)
    capture_enterer_name: Mapped[bool] = mapped_column(Boolean, default=True)
    disclose_owner_identity: Mapped[bool] = mapped_column(Boolean, default=False)
    secret_hash: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IntakeSession(Base):
    """One redeem: a non-owner session row carrying the per-session principal +
    a snapshot of the link config at open."""

    __tablename__ = "intake_sessions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.intake_links.id", ondelete="CASCADE")
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="drafting")
    # The running interview (W3): a list of {role, text}. Copied onto the submission at
    # capture; the owner browses it for in-progress/abandoned sessions (#15).
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    # Per-session cumulative backstops (§5): the loop's guardrails are per-turn only.
    turns_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_tokens_used: Mapped[int] = mapped_column(BigInteger, default=0)
    last_turn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The per-session turn lock (the concurrency cap, migration 0110): claimed atomically
    # before a turn streams, released after; a stale lock self-heals on the next claim.
    in_flight: Mapped[bool] = mapped_column(Boolean, default=False)


class IntakeSubmission(Base):
    """A captured submission + full transcript, materialized owner-side as a Proposal."""

    __tablename__ = "intake_submissions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.intake_links.id", ondelete="CASCADE")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.intake_sessions.id", ondelete="CASCADE")
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    enterer_name: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[list] = mapped_column(JSONB, default=list)
    draft: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="submitted")
    # The materialized owner Proposal (W4). The DB FK to app.proposals lives in the
    # migration; the ORM column omits it so this model needn't pull the proposals table
    # into the metadata just to INSERT a submission (proposal_id is set later, owner-side).
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    note_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
