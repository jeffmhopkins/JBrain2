"""jerv's per-conversation plan ORM + repo (migrations 0155, 0157, 0158).

`agent_session_plans` holds MANY owner-only plans per chat, each keyed by its own
`plan_id`: the plan `body` jerv authors and rewrites, a `status` lifecycle
(`not_approved | approved | in_work`), and an append-only `results` scratchpad. A
conversation can run several plans over its life; the "active" plan is the latest-created
one, and that is what the tools, the re-injection, the continuation loop, and the omnibox
all operate on. An older plan's row stays intact — its inline card keeps its own history —
but only the active plan ever carries a pending continuation (creating a new plan clears
the previous one's), so the session-scoped loop and the active plan never disagree.

`PlanRepo` takes the caller's already-RLS-scoped `AsyncSession` directly (the handler
owns the session/transaction), so the owner-only firewall is Postgres', not these
methods'. A plan is conversation-local, NOT knowledge-base data — hence no domain, and
jerv (empty-scoped) can still reach it because the firewall is `app.is_owner()`.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Text, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
# The first UNchecked checkbox, captured so `tick_next_step` can flip exactly it to `[x]`.
_FIRST_UNCHECKED = re.compile(r"^(\s*[-*+]\s+\[)( )(\])", re.MULTILINE)
# A list item that is NOT already a checkbox: a bullet with content but no `[ ]`/`[x]`.
# `normalize_plan_body` turns each of these into a top-level `- [ ]` step, so a plan is
# always a flat checklist the card can render and the loop can tick.
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?!\[[ xX]\])(.+?)\s*$")
# An already-checkbox line (any indent / bullet char) → re-emit at top level, `[ ]`/`[x]`.
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*?)\s*$")
# A heading/label line that introduces explanatory PROSE, not steps — "Notes", "Caveats",
# etc., as a markdown heading (`## Notes`), a bold label (`**Notes:**`), or a bare `Notes:`.
# Once a plan body hits one, the bullets under it are notes jerv added (caveats, reminders),
# NOT checklist steps — so `normalize_plan_body` leaves them as prose instead of flattening
# them into `- [ ]` steps, which had inflated the step count and fed non-actions ("Await your
# approval before starting") into the continuation loop. Matches a line that is ESSENTIALLY
# just the label (only `#`/`*`/`_`/`:` decoration around one notes-word), so a real step
# ("- [ ] Note the deadline") never trips it.
_PROSE_HEADING = re.compile(
    r"^\s*[#*_\s]*"
    r"(?:notes?|caveats?|context|background|assumptions?|references?|tips?|reminders?|fyi"
    r"|disclaimers?)"
    r"[\s:*_]*$",
    re.IGNORECASE,
)


def has_open_checklist_item(body: str) -> bool:
    """Whether the plan body still has an unchecked `- [ ]` step — the signal that the
    auto-continuation loop has more to do."""
    return bool(_UNCHECKED.search(body or ""))


def tick_next_step(body: str) -> str:
    """Flip the FIRST unchecked `- [ ]` to `- [x]` — the deterministic step check-off that
    `write_plan_result` does when jerv records a step's result, so progress no longer depends
    on jerv remembering to rewrite the body. No-op if nothing is unchecked."""
    return _FIRST_UNCHECKED.sub(r"\1x\3", body or "", count=1)


def normalize_plan_body(body: str) -> str:
    """Normalize a plan into a FLAT `- [ ]` checklist so the card renders it cleanly and the
    loop can tick each step (the Step-4-as-a-heading-with-nested-subitems render bug). Every
    list item — a bare bullet, an indented sub-bullet, or an existing checkbox at any indent —
    becomes a top-level `- [ ]` / `- [x]` step; a bullet that was already a checkbox keeps its
    checked state. Headings (`#…`), blank lines, and non-bullet prose pass through untouched,
    so the title and any framing survive. Idempotent.

    EXCEPTION — a trailing notes section (`**Notes**`, `## Caveats`, …): once a `_PROSE_HEADING`
    line is seen, the rest of the body is jerv's explanatory prose (caveats, reminders), NOT
    steps, so its bullets pass through UNCONVERTED. Otherwise those note bullets became checklist
    steps — inflating the count and feeding non-actions like "Await your approval before starting"
    into the continuation loop. The frontend's `parsePlanBody` already renders non-checkbox lines
    as prose, so this makes the notes show as notes, not steps."""
    if not body:
        return body
    out: list[str] = []
    in_prose = False
    for line in body.split("\n"):
        if _PROSE_HEADING.match(line):
            in_prose = True
            out.append(line)
            continue
        # In a notes section, leave every line verbatim — a bullet there is a note, not a step.
        if in_prose:
            out.append(line)
            continue
        cb = _CHECKBOX.match(line)
        if cb:
            mark = "x" if cb.group(1).lower() == "x" else " "
            out.append(f"- [{mark}] {cb.group(2)}")
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append(f"- [ ] {bullet.group(2)}")
            continue
        out.append(line)
    return "\n".join(out)


class AgentSessionPlan(Base):
    """One plan of a conversation — the active (latest) one is re-injected each jerv turn as
    the operating plan, rewritten by jerv, approved by the owner. Many rows may share a
    `session_id`; `plan_id` is the per-plan identity a card binds to."""

    __tablename__ = "agent_session_plans"
    __table_args__ = {"schema": "app"}

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
        # Session-scoped lookups are served by migration 0158's composite
        # `(session_id, created_at DESC)` index — no separate single-column index.
    )
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="not_approved")
    # The append-only step-results scratchpad (migration 0157): a JSONB array of
    # `{heading?, note}` entries, one per `write_plan_result` call, in append order. A later
    # step can add an entry but never overwrite an earlier one, so no turn erases prior
    # results; `read_plan` and the per-turn re-injection return the whole array.
    results: Mapped[list] = mapped_column(JSONB, default=list)
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
    """Reads/writes plans for one chat on a caller-supplied RLS-scoped session. Mutations key
    on a specific `plan_id`; the session-scoped paths (the tools, the loop, the re-injection)
    resolve the ACTIVE (latest-created) plan first with `get_active`/`active_plan_id`."""

    # --- reads -----------------------------------------------------------------

    async def get_by_id(self, session: AsyncSession, plan_id: str) -> AgentSessionPlan | None:
        """Fetch one specific plan by its `plan_id` — what an inline card polls so it keeps
        showing ITS plan's own body/status/results, not the session's current one."""
        return await session.get(AgentSessionPlan, uuid.UUID(plan_id))

    def _active_select(self, session_id: str):
        return (
            select(AgentSessionPlan)
            .where(AgentSessionPlan.session_id == uuid.UUID(session_id))
            # Latest-created is active; plan_id breaks a same-instant tie deterministically.
            .order_by(AgentSessionPlan.created_at.desc(), AgentSessionPlan.plan_id.desc())
            .limit(1)
        )

    async def get_active(self, session: AsyncSession, session_id: str) -> AgentSessionPlan | None:
        """The session's ACTIVE plan — the latest-created one — or None if it has no plan yet.
        This is 'the plan' for every session-scoped path (tools, re-injection, the loop)."""
        return (await session.execute(self._active_select(session_id))).scalars().first()

    async def active_plan_id(self, session: AsyncSession, session_id: str) -> str | None:
        """The active plan's id (or None) — for session-scoped callers that then mutate by id."""
        stmt = self._active_select(session_id).with_only_columns(AgentSessionPlan.plan_id)
        row = (await session.execute(stmt)).scalar_one_or_none()
        return str(row) if row is not None else None

    # --- writes ----------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        body: str = "",
        status: str = "not_approved",
    ) -> AgentSessionPlan:
        """Start a NEW plan for the conversation (its own plan_id, empty results). Creating it
        SUPERSEDES the prior plan: any pending continuation on the session's other plans is
        cleared, so only this new (now active) plan can auto-continue — the session-scoped loop
        never fires an older plan while a newer one is active."""
        stmt = (
            pg_insert(AgentSessionPlan)
            .values(session_id=uuid.UUID(session_id), body=body, status=status)
            .returning(AgentSessionPlan)
        )
        plan = (await session.execute(stmt)).scalar_one()
        await session.execute(
            update(AgentSessionPlan)
            .where(
                AgentSessionPlan.session_id == uuid.UUID(session_id),
                AgentSessionPlan.plan_id != plan.plan_id,
                AgentSessionPlan.continuation_due_at.isnot(None),
            )
            .values(continuation_due_at=None, updated_at=func.now())
        )
        return plan

    async def update_plan(
        self,
        session: AsyncSession,
        plan_id: str,
        *,
        body: str | None = None,
        status: str | None = None,
    ) -> AgentSessionPlan | None:
        """Update an EXISTING plan by id, changing only the fields provided (the tools editing
        the active plan in place, and the owner's edit-before-approve). Returns None if the
        plan is gone."""
        values: dict[str, object] = {"updated_at": func.now()}
        if body is not None:
            values["body"] = body
        if status is not None:
            values["status"] = status
        stmt = (
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(**values)
            .returning(AgentSessionPlan)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def set_status(
        self, session: AsyncSession, plan_id: str, status: str
    ) -> AgentSessionPlan | None:
        """Set a plan's status by id (jerv's in-work flip). Returns None if the plan is gone."""
        return await self.update_plan(session, plan_id, status=status)

    async def approve(self, session: AsyncSession, plan_id: str) -> AgentSessionPlan | None:
        """The owner's approve transition on a specific plan: move it to `approved` AND clear any
        stale `awaiting_owner`. jerv may have paused the draft (a not_approved plan is already
        waiting for approval), and a leftover await-owner flag would block the very first
        continuation the approve endpoint arms next — `claim_due_continuations` skips
        awaiting-owner plans — leaving an approved plan that never starts. Returns None if the
        plan is gone (the owner can't approve a plan that was never drafted)."""
        stmt = (
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(status="approved", awaiting_owner=False, updated_at=func.now())
            .returning(AgentSessionPlan)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def complete_step(
        self, session: AsyncSession, plan_id: str, *, note: str, heading: str | None = None
    ) -> AgentSessionPlan | None:
        """Record a finished step on a specific plan: APPEND `{heading?, note}` to the results
        scratchpad (never rewriting an earlier entry, so nothing is erased), AND deterministically
        advance the plan — tick the first unchecked `- [ ]` to `- [x]` and flip `approved` →
        `in_work`. This is the "I finished a step" signal, so progress no longer depends on jerv
        separately remembering to check the box or set the status. Read-modify-write is safe
        because turns are serialized per plan (the single-turn guard + the sweep's atomic claim).
        Returns None if the plan is gone."""
        plan = await self.get_by_id(session, plan_id)
        if plan is None:
            return None
        entry: dict[str, str] = {"note": note}
        if heading:
            entry["heading"] = heading
        results = [*(plan.results or []), entry]
        stmt = (
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(
                results=results,
                body=tick_next_step(plan.body),
                status="in_work" if plan.status == "approved" else plan.status,
                updated_at=func.now(),
            )
            .returning(AgentSessionPlan)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # --- auto-continuation bookkeeping -----------------------------------------

    async def schedule_continuation(
        self, session: AsyncSession, plan_id: str, *, delay_s: int
    ) -> None:
        """Arm a continuation on a specific plan `delay_s` from now (the owner-interruptible
        window). The due-time is computed against the DB clock so it survives a restart — the
        sweep claims it whenever it next comes due."""
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(
                continuation_due_at=func.now() + timedelta(seconds=delay_s),
                updated_at=func.now(),
            )
        )

    async def claim_due_continuations(
        self, session: AsyncSession, *, max_continuations: int
    ) -> list[tuple[str, str]]:
        """Atomically claim every plan whose continuation is due — clearing the due-time in
        one UPDATE, so a concurrent sweep (or a `/continue` POST) can't fire the same one
        twice. Only in-work/approved, not-awaiting-owner, under-cap plans are claimed. Returns
        `(plan_id, session_id)` pairs. The used counter is bumped by `bump_continuation` when the
        turn actually runs, NOT here — so a claim that is then skipped (session busy, plan
        changed) never burns a continuation against the cap."""
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
            .returning(AgentSessionPlan.plan_id, AgentSessionPlan.session_id)
        )
        rows = (await session.execute(stmt)).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    async def bump_continuation(self, session: AsyncSession, plan_id: str) -> None:
        """Count one continuation fire — called when a claimed continuation actually runs a
        turn (not at claim time), so the cap counts real runs, not skipped claims."""
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(
                continuations_used=AgentSessionPlan.continuations_used + 1,
                updated_at=func.now(),
            )
        )

    async def cancel_and_reset(self, session: AsyncSession, plan_id: str) -> None:
        """The owner-message reset (human-anchored): drop any pending continuation, clear
        the await-owner flag, and zero the cap counter — so the owner sending anything
        both cancels the timer and gives the loop a fresh budget."""
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(
                continuation_due_at=None,
                awaiting_owner=False,
                continuations_used=0,
                updated_at=func.now(),
            )
        )

    async def set_awaiting_owner(
        self, session: AsyncSession, plan_id: str, value: bool = True
    ) -> None:
        """jerv's explicit opt-out: mark the plan as awaiting the owner (suppressing the
        auto-loop) and clear any pending continuation. Clearing it (value=False) lets the
        loop resume without touching the due-time."""
        values: dict[str, object] = {"awaiting_owner": value, "updated_at": func.now()}
        if value:
            values["continuation_due_at"] = None
        await session.execute(
            update(AgentSessionPlan)
            .where(AgentSessionPlan.plan_id == uuid.UUID(plan_id))
            .values(**values)
        )
