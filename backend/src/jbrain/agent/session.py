"""Agent sessions: the capability record (selected read scope), the RLS context a
session's tools read under, and the action-policy lookup.

A session is owner-only metadata. Managing sessions (create/list) runs as the
full-scope owner; a session's *reads* run as the owner narrowed to its selected
domains via the `owner_scoped` firewall (migration 0015) — enforced by Postgres,
not by the tools (docs/ASSISTANT.md "Session capabilities").
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import DEFAULT_OWNER_POLICY, PermissionClass, PolicyOutcome
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import AgentSession, AgentTurn
from jbrain.models.proposals import Proposal

_PREVIEW_LEN = 140  # the resume hint on a chat card; longer is clamped in the UI too


@dataclass(frozen=True)
class AgentSessionInfo:
    id: str
    title: str
    status: str
    domain_scopes: tuple[str, ...]
    subject_ids: tuple[str, ...]
    created_at: datetime
    last_active_at: datetime
    # The selected agent persona (docs/ASSISTANT.md "Agent selection"). Defaulted so
    # existing callers/tests that predate agent selection resolve to the curator.
    agent: str = "curator"
    # Sub-agent lineage (docs/SUBAGENT_SPAWNING_PLAN.md). A root chat leaves these at
    # their defaults; a spawned child carries its parent + depth + sandbox flag.
    parent_session_id: str | None = None
    depth: int = 0
    no_memory: bool = False
    # List-view metadata (the Chats cards). Populated by `list`; the single-row
    # `get`/`create` paths leave them at their resting defaults — the chat
    # endpoint doesn't need them.
    turn_count: int = 0
    preview: str = ""
    staged_count: int = 0


def _info(
    row: AgentSession,
    *,
    turn_count: int = 0,
    preview: str = "",
    staged_count: int = 0,
) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=str(row.id),
        title=row.title,
        status=row.status,
        agent=row.agent,
        parent_session_id=str(row.parent_session_id) if row.parent_session_id else None,
        depth=row.depth,
        no_memory=row.no_memory,
        domain_scopes=tuple(row.domain_scopes),
        subject_ids=tuple(str(s) for s in row.subject_ids),
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        turn_count=turn_count,
        preview=preview,
        staged_count=staged_count,
    )


def read_context(principal_id: str, scopes: Sequence[str]) -> SessionContext:
    """The RLS context a session's tools read under: owner identity (so owner-only
    tables stay visible) narrowed to `scopes` via owner_scoped. A health-only
    session physically cannot read a finance row (invariant #4)."""
    return SessionContext(
        principal_id=principal_id,
        principal_kind="owner",
        domain_scopes=tuple(scopes),
        owner_scoped=True,
    )


def outcome_for(
    permission: PermissionClass,
    policy: Mapping[PermissionClass, PolicyOutcome] = DEFAULT_OWNER_POLICY,
) -> PolicyOutcome:
    """What a session does with a tool of this permission class: run it now
    (`direct`), stage a Proposal (`staged`), or refuse it (`denied`). The loop
    consults this before dispatching a tool call."""
    return policy[permission]


class AgentSessionRepo:
    """CRUD for agent sessions, on RLS-scoped sessions. Session management runs as
    the owner (full scope); the read narrowing applies to a session's tool reads,
    not to the session list itself."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def create(
        self,
        ctx: SessionContext,
        *,
        domain_scopes: Sequence[str],
        subject_ids: Sequence[str] = (),
        title: str = "",
        agent: str = "curator",
        parent_session_id: str | None = None,
        depth: int = 0,
        no_memory: bool = False,
    ) -> AgentSessionInfo:
        async with scoped_session(self._maker, ctx) as session:
            row = AgentSession(
                principal_id=uuid.UUID(ctx.principal_id),
                title=title,
                agent=agent,
                parent_session_id=uuid.UUID(parent_session_id) if parent_session_id else None,
                depth=depth,
                no_memory=no_memory,
                domain_scopes=list(domain_scopes),
                subject_ids=[uuid.UUID(s) for s in subject_ids],
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def list(self, ctx: SessionContext) -> list[AgentSessionInfo]:
        # Each card carries its turn count, a resume preview (the latest turn,
        # clamped), and how many Proposals it has staged — three correlated
        # subqueries so the list is one round-trip, all under the owner's RLS.
        turn_count = (
            select(func.count())
            .select_from(AgentTurn)
            .where(AgentTurn.session_id == AgentSession.id, AgentTurn.role == "user")
            .scalar_subquery()
        )
        preview = (
            select(func.left(AgentTurn.content, _PREVIEW_LEN))
            .where(AgentTurn.session_id == AgentSession.id)
            .order_by(AgentTurn.seq.desc())
            .limit(1)
            .scalar_subquery()
        )
        staged_count = (
            select(func.count())
            .select_from(Proposal)
            .where(Proposal.session_id == AgentSession.id, Proposal.status == "staged")
            .scalar_subquery()
        )
        async with scoped_session(self._maker, ctx) as session:
            rows = await session.execute(
                select(AgentSession, turn_count, preview, staged_count).order_by(
                    AgentSession.last_active_at.desc()
                )
            )
            return [
                _info(row, turn_count=tc, preview=pv or "", staged_count=sc)
                for row, tc, pv, sc in rows
            ]

    async def get(self, ctx: SessionContext, session_id: str) -> AgentSessionInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = await session.get(AgentSession, uuid.UUID(session_id))
            return _info(row) if row is not None else None

    async def touch(self, ctx: SessionContext, session_id: str) -> None:
        """Mark a session active now — drives the Sessions-page ordering."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(AgentSession)
                .where(AgentSession.id == uuid.UUID(session_id))
                .values(last_active_at=datetime.now(UTC))
            )

    async def end(self, ctx: SessionContext, session_id: str) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(AgentSession)
                .where(AgentSession.id == uuid.UUID(session_id))
                .values(status="ended")
            )

    async def set_status(self, ctx: SessionContext, session_id: str, status: str) -> None:
        """Flip a session's lifecycle status (archived ⇄ active) — archiving tidies a
        chat out of the live list without deleting it or its transcript."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(AgentSession)
                .where(AgentSession.id == uuid.UUID(session_id))
                .values(status=status)
            )

    async def set_scopes(
        self, ctx: SessionContext, session_id: str, domain_scopes: Sequence[str]
    ) -> None:
        """Re-scope a session after start (owner-only — the endpoint is owner-gated,
        and RLS still enforces the firewall per query). Scope is a rail the owner
        nudges, not a gate frozen at creation (docs/ASSISTANT.md "Sessions")."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(AgentSession)
                .where(AgentSession.id == uuid.UUID(session_id))
                .values(domain_scopes=list(domain_scopes))
            )

    async def rename(self, ctx: SessionContext, session_id: str, title: str) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(AgentSession)
                .where(AgentSession.id == uuid.UUID(session_id))
                .values(title=title)
            )

    async def delete(self, ctx: SessionContext, session_id: str) -> None:
        """Remove the session; the run log and transcript cascade with it."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                delete(AgentSession).where(AgentSession.id == uuid.UUID(session_id))
            )
