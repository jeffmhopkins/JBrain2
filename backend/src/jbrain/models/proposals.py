"""Proposal ORM models (docs/reference/ASSISTANT.md "Staging & approval"). A Proposal is a
tree of staged operations the owner approves in whole or in part; nodes declare
prerequisites so partial approval stays dependency-safe."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class Proposal(Base):
    """One staged unit of work — owner-only, single-domain, the unified
    review-inbox item for agent-proposed changes."""

    __tablename__ = "proposals"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_sessions.id", ondelete="SET NULL"), nullable=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.principals.id")
    )
    kind: Mapped[str] = mapped_column(Text)  # correction | knowledge | wiki-restructure | ...
    status: Mapped[str] = mapped_column(Text, default="staged", server_default="staged")
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The conversation/notes/attachments that prompted this, by id (#7 provenance).
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProposalNode(Base):
    """A node in a Proposal tree: a grouping node or an atomic leaf op. `deps`
    lists prerequisite node ids; `preview` is the rendered effect the owner
    judges; `status` is per-node so the tree is approvable in part."""

    __tablename__ = "proposal_nodes"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.proposals.id", ondelete="CASCADE")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.proposal_nodes.id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text)  # group | leaf
    op: Mapped[str] = mapped_column(Text, default="", server_default="")
    label: Mapped[str] = mapped_column(Text, default="", server_default="")
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    deps: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
