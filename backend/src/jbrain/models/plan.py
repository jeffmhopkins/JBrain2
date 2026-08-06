"""jerv's per-conversation plan ORM + repo (migration 0155).

`agent_session_plans` is one owner-only row per chat: the plan `body` jerv authors
and rewrites, plus a `status` lifecycle (`not_approved | approved | in_work`). jerv
reads/writes the body and may set `not_approved`/`in_work`; the `approved` transition
is the OWNER's alone (the api approve endpoint), never a jerv tool — the state-machine
rule lives in the tool handler, this repo just persists what it's told.

`PlanRepo` takes the caller's already-RLS-scoped `AsyncSession` directly (the handler
owns the session/transaction), so the owner-only firewall is Postgres', not these
methods'. A plan is conversation-local, NOT knowledge-base data — hence no domain, and
jerv (empty-scoped) can still reach it because the firewall is `app.is_owner()`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base

# The lifecycle statuses, in draft→approved→in-work order. `not_approved` is the
# default a fresh draft starts at; only the owner endpoint sets `approved`.
PLAN_STATUSES = ("not_approved", "approved", "in_work")


class AgentSessionPlan(Base):
    """One conversation's plan — read at every jerv turn (when approved/in-work it is
    re-injected as the operating plan), rewritten by jerv, approved by the owner."""

    __tablename__ = "agent_session_plans"
    __table_args__ = {"schema": "app"}

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="not_approved")
    # Auto-continuation bookkeeping (see the migration). due_at NULL = no continuation
    # pending; awaiting_owner suppresses it; continuations_used caps the chain.
    continuation_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    awaiting_owner: Mapped[bool] = mapped_column(default=False)
    continuations_used: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanRepo:
    """Reads/writes the plan row for one chat on a caller-supplied RLS-scoped session."""

    async def get(self, session: AsyncSession, session_id: str) -> AgentSessionPlan | None:
        return await session.get(AgentSessionPlan, uuid.UUID(session_id))

    async def upsert(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        body: str | None = None,
        status: str | None = None,
    ) -> AgentSessionPlan:
        """Create-or-update the plan, changing only the fields provided. On insert the
        unset field takes its column default (`body=''`, `status='not_approved'`). The
        caller (the tool handler / the approve endpoint) is responsible for the
        state-machine rules — this only persists."""
        values: dict[str, object] = {"session_id": uuid.UUID(session_id), "updated_at": func.now()}
        if body is not None:
            values["body"] = body
        if status is not None:
            values["status"] = status
        set_ = {k: v for k, v in values.items() if k != "session_id"}
        stmt = (
            pg_insert(AgentSessionPlan)
            .values(**values)
            .on_conflict_do_update(index_elements=[AgentSessionPlan.session_id], set_=set_)
            .returning(AgentSessionPlan)
            # A prior get() in this session caches the row in the identity map; without
            # populate_existing the ORM returns that stale instance instead of the values
            # RETURNING just produced (so an in-place status change would read stale).
            .execution_options(populate_existing=True)
        )
        row = (await session.execute(stmt)).scalar_one()
        return row

    async def set_status(
        self, session: AsyncSession, session_id: str, status: str
    ) -> AgentSessionPlan | None:
        """Set the status of an EXISTING plan (the owner approve path, and jerv's
        in-work flip). Returns None if no plan exists — the owner cannot approve a plan
        that was never drafted."""
        stmt = (
            update(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            .values(status=status, updated_at=func.now())
            .returning(AgentSessionPlan)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()
