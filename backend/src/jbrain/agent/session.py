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

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import DEFAULT_OWNER_POLICY, PermissionClass, PolicyOutcome
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import AgentSession


@dataclass(frozen=True)
class AgentSessionInfo:
    id: str
    title: str
    status: str
    domain_scopes: tuple[str, ...]
    subject_ids: tuple[str, ...]
    created_at: datetime
    last_active_at: datetime


def _info(row: AgentSession) -> AgentSessionInfo:
    return AgentSessionInfo(
        id=str(row.id),
        title=row.title,
        status=row.status,
        domain_scopes=tuple(row.domain_scopes),
        subject_ids=tuple(str(s) for s in row.subject_ids),
        created_at=row.created_at,
        last_active_at=row.last_active_at,
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
    ) -> AgentSessionInfo:
        async with scoped_session(self._maker, ctx) as session:
            row = AgentSession(
                principal_id=uuid.UUID(ctx.principal_id),
                title=title,
                domain_scopes=list(domain_scopes),
                subject_ids=[uuid.UUID(s) for s in subject_ids],
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def list(self, ctx: SessionContext) -> list[AgentSessionInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(AgentSession).order_by(AgentSession.last_active_at.desc())
                )
            ).scalars()
            return [_info(r) for r in rows]

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
