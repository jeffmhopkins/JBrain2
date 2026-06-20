"""Chat-turn attachments: the files a user attaches to a Full Brain chat turn.

Every query runs on an RLS-scoped session, so the domain firewall is enforced by
Postgres, not by these methods (CLAUDE.md rule 3). Files are pre-uploaded and
referenced by id: a row is linked to the SESSION at upload and bound to the user
`turn_id` only when that turn is recorded (Stage-2 Wave 2).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import TurnAttachment

# A chat file whose session has no single domain falls back to this shared scope:
# a Jerv/Teacher session (empty scopes) or a multi/all-domain session must not
# stamp its file with one privileged domain (which would over- or under-expose it).
DEFAULT_ATTACHMENT_DOMAIN = "general"

# The media types a chat attachment may carry — the SINGLE source of truth shared by
# the upload endpoint (which rejects anything else with 415) and the LLM-content
# conversion (attachment_content.py), so the two can never drift. Vision-readable
# images, PDFs (rasterized + text-extracted), and the plain-text families the model
# reads inline. Anything not here has no conversion path, so it must not be stored.
ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }
)


def is_allowed_media_type(media_type: str) -> bool:
    """Whether a chat attachment of this media type may be stored. The upload
    endpoint rejects the rest with 415 (an out-of-allowlist file would have no
    conversion path on the chat-send side anyway)."""
    return media_type in ALLOWED_MEDIA_TYPES


def domain_for_session(domain_scopes: Sequence[str]) -> str:
    """The firewall domain a chat attachment is stamped with, from its session's
    scopes (owner decision). EXACTLY ONE scope → that domain (the file inherits the
    session's single firewall). ZERO scopes (Jerv/Teacher, full owner) or MULTIPLE
    scopes → 'general': there is no single domain to inherit, so the file goes to the
    shared scope rather than guessing one. SECURITY-relevant: this domain decides
    which later sessions can read the file via has_domain_scope RLS."""
    return domain_scopes[0] if len(domain_scopes) == 1 else DEFAULT_ATTACHMENT_DOMAIN


@dataclass(frozen=True)
class AttachmentInfo:
    id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    domain_code: str
    has_extracts: bool = False
    has_description: bool = False


def _info(a: TurnAttachment) -> AttachmentInfo:
    return AttachmentInfo(
        id=str(a.id),
        filename=a.filename,
        media_type=a.media_type,
        size_bytes=a.size_bytes,
        sha256=a.sha256,
        domain_code=a.domain_code,
        has_extracts=a.has_extracts,
        has_description=a.has_description,
    )


class TurnAttachmentRepo:
    """CRUD for chat-turn attachments on RLS-scoped sessions. The endpoint computes
    the file's domain from the session's scopes (domain_for_session) and reads under
    the session's narrowed firewall (agent.session.read_context), so a scoped session
    physically cannot stamp or read an out-of-scope attachment."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], sessions: AgentSessionRepo):
        self._maker = maker
        self._sessions = sessions

    async def session_read_context(
        self, owner_ctx: SessionContext, session_id: str
    ) -> SessionContext | None:
        """The narrowed RLS context a chat attachment is written/read under: the
        owner narrowed to the session's selected scopes. None when the session is
        not visible to `owner_ctx`."""
        info = await self._sessions.get(owner_ctx, session_id)
        if info is None:
            return None
        return read_context(owner_ctx.principal_id, info.domain_scopes)

    async def add(
        self,
        ctx: SessionContext,
        session_id: str,
        *,
        sha256: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        domain_code: str,
    ) -> AttachmentInfo:
        async with scoped_session(self._maker, ctx) as session:
            row = TurnAttachment(
                session_id=uuid.UUID(session_id),
                domain_code=domain_code,
                sha256=sha256,
                filename=filename,
                media_type=media_type,
                size_bytes=size_bytes,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _info(row)

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    select(TurnAttachment).where(TurnAttachment.id == attachment_id)
                )
            ).scalar_one_or_none()
            return None if row is None else _info(row)

    async def remove(self, ctx: SessionContext, attachment_id: str) -> str | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    select(TurnAttachment).where(TurnAttachment.id == attachment_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # The blob stays: content-addressed storage may share it; only the link goes.
            await session.delete(row)
            return attachment_id

    async def list_for_session(self, ctx: SessionContext, session_id: str) -> list[AttachmentInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(TurnAttachment)
                        .where(TurnAttachment.session_id == session_id)
                        .order_by(TurnAttachment.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_info(r) for r in rows]

    async def list_for_turns(
        self, ctx: SessionContext, turn_ids: Sequence[str]
    ) -> dict[str, list[AttachmentInfo]]:
        """The attachments bound to each of `turn_ids`, keyed by turn id — for
        transcript replay (one round-trip for the whole transcript). RLS-scoped, so a
        turn's out-of-scope attachment never appears; turns with none are absent from
        the map. Ordered by upload time within a turn for a stable replay."""
        if not turn_ids:
            return {}
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(TurnAttachment)
                        .where(TurnAttachment.turn_id.in_([uuid.UUID(t) for t in turn_ids]))
                        .order_by(TurnAttachment.created_at)
                    )
                )
                .scalars()
                .all()
            )
        by_turn: dict[str, list[AttachmentInfo]] = {}
        for row in rows:
            by_turn.setdefault(str(row.turn_id), []).append(_info(row))
        return by_turn

    async def bind_to_turn(
        self, ctx: SessionContext, attachment_ids: Sequence[str], turn_id: str
    ) -> None:
        """Bind pre-uploaded attachments to the user turn that referenced them, once
        that turn is recorded (pre-upload, reference-by-id). Runs under the session's
        narrowed context, so RLS physically restricts the UPDATE to in-scope rows — an
        id outside the session's firewall simply isn't matched, never bound."""
        if not attachment_ids:
            return
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(TurnAttachment)
                .where(TurnAttachment.id.in_([uuid.UUID(a) for a in attachment_ids]))
                .values(turn_id=uuid.UUID(turn_id))
            )
