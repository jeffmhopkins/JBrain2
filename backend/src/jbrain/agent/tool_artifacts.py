"""Cross-turn tool-result artifacts: the by-reference memory jerv keeps of an expensive
tool result (a fetched web page, a YouTube caption transcript) so it survives across
conversation turns (docs/plans/CROSS_TURN_TOOL_RESULTS_PLAN.md).

Every query runs on an RLS-scoped session, so the domain firewall is enforced by
Postgres, not by these methods (CLAUDE.md rule 3). The heavy text is content-addressed
in the blob store (invariant #2); this row holds only metadata + the paging cursor.

The firewall model is the SAME as a chat attachment (jbrain.agent.attachments): the
artifact is stamped with the session's single domain, or 'general' for an empty/multi/
all-scope session (jerv is empty-scoped) — so `has_domain_scope` decides which later
sessions can read it, and a scoped session physically cannot stamp or read out of scope.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import TurnToolArtifact

# The shared fallback scope for a session with no single domain — identical to a chat
# attachment's (jbrain.agent.attachments.DEFAULT_ATTACHMENT_DOMAIN). An artifact must
# follow the exact same rule so jerv's empty-scope sessions can read their own artifacts.
DEFAULT_ARTIFACT_DOMAIN = "general"


def artifact_domain(domain_scopes: Sequence[str]) -> str:
    """The firewall domain an artifact is stamped with, from its session's scopes.
    EXACTLY ONE scope → that domain; ZERO (jerv/Teacher) or MULTIPLE → 'general'. This is
    the SECURITY choice deciding which later sessions can read it (see attachments)."""
    return domain_scopes[0] if len(domain_scopes) == 1 else DEFAULT_ARTIFACT_DOMAIN


def artifact_scopes(session_scopes: Sequence[str]) -> tuple[str, ...]:
    """The RLS scopes an artifact is written/read under: the session's own scopes PLUS the
    domain it is stamped with. For a single scope this is unchanged (a finance session
    still can't reach a health artifact); for zero/multiple it adds 'general' so the
    INSERT's WITH CHECK passes and the later read finds its own artifact."""
    return tuple({*session_scopes, artifact_domain(session_scopes)})


@dataclass(frozen=True)
class ToolArtifact:
    id: str
    kind: str
    source_url: str
    title: str
    sha256: str
    total_chars: int
    last_offset: int
    domain_code: str


def _info(a: TurnToolArtifact) -> ToolArtifact:
    return ToolArtifact(
        id=str(a.id),
        kind=a.kind,
        source_url=a.source_url,
        title=a.title,
        sha256=a.sha256,
        total_chars=a.total_chars,
        last_offset=a.last_offset,
        domain_code=a.domain_code,
    )


class ToolArtifactRepo:
    """CRUD for cross-turn tool artifacts on RLS-scoped sessions. `session_context`
    resolves the narrowed firewall context (owner → session scopes + stamped domain) an
    artifact is written/read under, exactly like TurnAttachmentRepo.session_read_context —
    so a scoped session physically cannot stamp or read an out-of-scope artifact."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], sessions: AgentSessionRepo):
        self._maker = maker
        self._sessions = sessions

    async def session_context(
        self, owner_ctx: SessionContext, session_id: str
    ) -> tuple[SessionContext, str] | None:
        """(narrowed RLS context, stamped domain_code) for this session's artifacts, or
        None when the session is not visible to `owner_ctx`. The context carries the
        session's scopes plus the stamped domain, so an empty/multi-scope (jerv) session
        can write and read its own 'general'-stamped artifacts; a single-scope session is
        unchanged."""
        info = await self._sessions.get(owner_ctx, session_id)
        if info is None:
            return None
        ctx = read_context(owner_ctx.principal_id, artifact_scopes(info.domain_scopes))
        return ctx, artifact_domain(info.domain_scopes)

    async def remember(
        self,
        ctx: SessionContext,
        session_id: str,
        *,
        kind: str,
        source_url: str,
        title: str,
        sha256: str,
        total_chars: int,
        domain_code: str,
    ) -> ToolArtifact | None:
        """Upsert the artifact for (session, source_url): a re-fetch of the same URL this
        session REFRESHES the one row (new text/length, cursor reset to 0) rather than
        piling up. RLS-scoped — a session that can't write this domain inserts nothing
        (returns None). Returns the stored artifact."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    select(TurnToolArtifact).where(
                        TurnToolArtifact.session_id == uuid.UUID(session_id),
                        TurnToolArtifact.source_url == source_url,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = TurnToolArtifact(
                    session_id=uuid.UUID(session_id),
                    domain_code=domain_code,
                    kind=kind,
                    source_url=source_url,
                    title=title,
                    sha256=sha256,
                    total_chars=total_chars,
                    last_offset=0,
                )
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return _info(row)
            row.kind = kind
            row.title = title
            row.sha256 = sha256
            row.total_chars = total_chars
            row.last_offset = 0
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def get(self, ctx: SessionContext, artifact_id: str) -> ToolArtifact | None:
        try:
            aid = uuid.UUID(artifact_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(select(TurnToolArtifact).where(TurnToolArtifact.id == aid))
            ).scalar_one_or_none()
            return None if row is None else _info(row)

    async def set_offset(self, ctx: SessionContext, artifact_id: str, last_offset: int) -> None:
        """Advance the paging cursor so a later `read_artifact` continues where it stopped.
        RLS-scoped: a session that can't see the row updates nothing."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(TurnToolArtifact)
                .where(TurnToolArtifact.id == uuid.UUID(artifact_id))
                .values(last_offset=max(0, last_offset))
            )

    async def list_for_session(
        self, ctx: SessionContext, session_id: str, *, limit: int
    ) -> list[ToolArtifact]:
        """The session's artifacts, most recent first, capped — for the cross-turn
        reference block. RLS-scoped, so an out-of-scope artifact never appears."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(TurnToolArtifact)
                        .where(TurnToolArtifact.session_id == uuid.UUID(session_id))
                        .order_by(TurnToolArtifact.created_at.desc())
                        .limit(max(1, limit))
                    )
                )
                .scalars()
                .all()
            )
            return [_info(r) for r in rows]
