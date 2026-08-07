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

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Text, func, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base

# The lifecycle statuses, in draft→approved→in-work order. `not_approved` is the
# default a fresh draft starts at; only the owner endpoint sets `approved`.
PLAN_STATUSES = ("not_approved", "approved", "in_work")

# An unchecked Markdown task line — `- [ ]` / `* [ ]` / `+ [ ]`. Its presence means the
# plan has work left, which is what gates a continuation (a fully `- [x]` plan is done).
_UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]", re.MULTILINE)


def has_open_checklist_item(body: str) -> bool:
    """Whether the plan body still has an unchecked `- [ ]` step — the signal that the
    auto-continuation loop has more to do."""
    return bool(_UNCHECKED.search(body or ""))


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

    async def approve(self, session: AsyncSession, session_id: str) -> AgentSessionPlan | None:
        """The owner's approve transition: move the plan to `approved` AND clear any stale
        `awaiting_owner`. jerv may have paused the draft (a not_approved plan is already
        waiting for approval), and a leftover await-owner flag would block the very first
        continuation the approve endpoint arms next — `claim_due_continuations` skips
        awaiting-owner plans — leaving an approved plan that never starts. Returns None if no
        plan exists (the owner can't approve a plan that was never drafted)."""
        stmt = (
            update(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            .values(status="approved", awaiting_owner=False, updated_at=func.now())
            .returning(AgentSessionPlan)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # --- auto-continuation bookkeeping -----------------------------------------

    async def schedule_continuation(
        self, session: AsyncSession, session_id: str, *, delay_s: int
    ) -> None:
        """Arm a continuation `delay_s` from now (the owner-interruptible window). The
        due-time is computed against the DB clock so it survives a restart — the sweep
        claims it whenever it next comes due."""
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            .values(
                continuation_due_at=func.now() + timedelta(seconds=delay_s),
                updated_at=func.now(),
            )
        )

    async def claim_due_continuations(
        self, session: AsyncSession, *, max_continuations: int
    ) -> list[str]:
        """Atomically claim every plan whose continuation is due — clearing the due-time in
        one UPDATE, so a concurrent sweep (or a `/continue` POST) can't fire the same one
        twice. Only in-work, not-awaiting-owner, under-cap plans are claimed. Returns the
        claimed session ids. The used counter is bumped by `bump_continuation` when the turn
        actually runs, NOT here — so a claim that is then skipped (session busy, plan changed)
        never burns a continuation against the cap."""
        stmt = (
            update(AgentSessionPlan)
            .where(
                AgentSessionPlan.continuation_due_at.isnot(None),
                AgentSessionPlan.continuation_due_at <= func.now(),
                # `approved` (the first step, kicked off by the approve endpoint) or
                # `in_work` (jerv already executing) — not a `not_approved` draft.
                AgentSessionPlan.status.in_(("approved", "in_work")),
                AgentSessionPlan.awaiting_owner.is_(False),
                AgentSessionPlan.continuations_used < max_continuations,
            )
            .values(continuation_due_at=None, updated_at=func.now())
            .returning(AgentSessionPlan.session_id)
        )
        rows = (await session.execute(stmt)).all()
        return [str(r[0]) for r in rows]

    async def bump_continuation(self, session: AsyncSession, session_id: str) -> None:
        """Count one continuation fire — called when a claimed continuation actually runs a
        turn (not at claim time), so the cap counts real runs, not skipped claims."""
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            .values(
                continuations_used=AgentSessionPlan.continuations_used + 1,
                updated_at=func.now(),
            )
        )

    async def cancel_and_reset(self, session: AsyncSession, session_id: str) -> None:
        """The owner-message reset (human-anchored): drop any pending continuation, clear
        the await-owner flag, and zero the cap counter — so the owner sending anything
        both cancels the timer and gives the loop a fresh budget."""
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            .values(
                continuation_due_at=None,
                awaiting_owner=False,
                continuations_used=0,
                updated_at=func.now(),
            )
        )

    async def set_awaiting_owner(
        self, session: AsyncSession, session_id: str, value: bool = True
    ) -> None:
        """jerv's explicit opt-out: mark the plan as awaiting the owner (suppressing the
        auto-loop) and clear any pending continuation. Clearing it (value=False) lets the
        loop resume without touching the due-time."""
        values: dict[str, object] = {"awaiting_owner": value, "updated_at": func.now()}
        if value:
            values["continuation_due_at"] = None
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            .values(**values)
        )
