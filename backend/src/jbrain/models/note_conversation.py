"""Note-conversation ORM + repo (migration 0191).

A note conversation is an ordinary `app.agent_sessions` row (D1 of
docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md); `note_conversations` is the side table
that makes one a note conversation, and `note_conversation_tool_calls` is the durable
per-tool-call ledger the D3 chip renders and constraint 6's whole-conversation
`touched`/`projected` accumulator reads back. W1 shipped `CommitOutcome` as an
in-memory dataclass precisely because that accumulation has to outlive a turn.

`NoteConversationRepo` takes the caller's already-RLS-scoped `AsyncSession` (the
`ArchivistMemoryRepo`/`PlanRepo` idiom): the handler owns the transaction, so the
owner-only firewall is Postgres', not these methods'.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Text, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import update

from jbrain.models.core import Base

# The closed lifecycle, defended in migration 0191's docstring.
NOTE_CONVERSATION_STATES = ("running", "waiting_on_owner", "settled", "failed")
# The two states that hold the note: the partial unique index admits exactly one of
# these per note. `settled` and `failed` release it so a new pass can start.
LIVE_STATES = ("running", "waiting_on_owner")

# Caps on a recorded call's `args`. The blob is stored, never executed — but a note body
# may be third-party text (risk 1) and the model copies note text into `quote` /
# `statement`, so an unbounded ledger is a disk-exhaustion lever a hostile body can pull
# on a box whose storage the owner cannot reclaim from a terminal. Truncation lives here
# rather than in a DB CHECK because a CHECK would abort the transaction that carries the
# graph write the call already made: losing the audit row is the lesser harm, losing the
# write is not.
MAX_ARG_CHARS = 2000
MAX_ARGS_CHARS = 16000
# Set on a capped blob so a reader never mistakes a truncated argument for what the model
# actually sent. Overwrites a same-named model argument, which is the safe direction.
TRUNCATED_KEY = "_truncated"


def note_body_sha(body: str) -> str:
    """The `note_body_sha` a conversation is opened against. D6 appends clarification
    blocks and that re-ingests the note, so a resumed pass compares this against the
    live body to learn the note moved under it."""
    return hashlib.sha256(body.encode()).hexdigest()


def _truncate(value: Any, flag: list[bool]) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_ARG_CHARS:
            flag[0] = True
            return value[:MAX_ARG_CHARS]
        return value
    if isinstance(value, dict):
        return {k: _truncate(v, flag) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v, flag) for v in value]
    return value


def cap_tool_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Bound one call's recorded arguments. Strings are truncated in place, at any
    nesting depth, because the batch shapes in TOOL_SURFACE.md put the quotes inside
    arrays of objects. A blob still over the total cap — many small elements clear the
    per-string cap and can still be huge — degrades to its key names: the chip needs
    the shape of the call, and the ledger needs to record that the call happened."""
    flag = [False]
    capped: dict[str, Any] = _truncate(dict(args), flag)
    if len(json.dumps(capped, default=str)) > MAX_ARGS_CHARS:
        capped = {"_keys": sorted(str(k) for k in args)}
        flag[0] = True
    if flag[0]:
        capped[TRUNCATED_KEY] = True
    return capped


class NoteConversation(Base):
    """One note's ingest conversation: the session it runs in, the note it reads, where
    it is in its lifecycle, and the body it was started against."""

    __tablename__ = "note_conversations"
    __table_args__ = {"schema": "app"}

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    note_body_sha: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteConversationToolCall(Base):
    """One recorded tool call of a note conversation — what the write CLAIMED, in call
    order. Append-only apart from `turn_id`, which is bound when the assistant turn is
    written at the end of the exchange."""

    __tablename__ = "note_conversation_tool_calls"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.note_conversations.session_id", ondelete="CASCADE"),
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_turns.id", ondelete="SET NULL"), nullable=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    name: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    entity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    fact_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    domains: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass(frozen=True)
class ConversationWrites:
    """The whole-conversation union of what its successful calls wrote — constraint 6's
    `touched`/`projected` sets, durable across turns. `settle_note` retracts every
    non-pinned fact of the note NOT in `facts`, so a per-turn share would retract the
    previous turn's commits; this is why the ledger exists."""

    facts: set[uuid.UUID] = field(default_factory=set)
    entities: set[uuid.UUID] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)


class NoteConversationRepo:
    """Reads/writes a note conversation and its tool-call ledger on a caller-supplied
    RLS-scoped session."""

    # --- the conversation ------------------------------------------------------

    async def start(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        note_id: str,
        body_sha: str,
        state: str = "running",
    ) -> NoteConversation:
        """Open a conversation for a note. Raises `IntegrityError` when the note already
        has a live one — the partial unique index, not a read-then-write check, is what
        makes that race-free."""
        stmt = (
            pg_insert(NoteConversation)
            .values(
                session_id=uuid.UUID(session_id),
                note_id=uuid.UUID(note_id),
                note_body_sha=body_sha,
                state=state,
            )
            .returning(NoteConversation)
        )
        return (await session.execute(stmt)).scalar_one()

    async def get(self, session: AsyncSession, session_id: str) -> NoteConversation | None:
        return await session.get(NoteConversation, uuid.UUID(session_id))

    async def live_for_note(self, session: AsyncSession, note_id: str) -> NoteConversation | None:
        """The note's live thread, or None. At most one exists by construction."""
        stmt = select(NoteConversation).where(
            NoteConversation.note_id == uuid.UUID(note_id),
            NoteConversation.state.in_(LIVE_STATES),
        )
        return (await session.execute(stmt)).scalars().first()

    async def list_in_state(
        self, session: AsyncSession, state: str, *, limit: int = 50
    ) -> list[NoteConversation]:
        """Conversations in one state, newest activity first — the notes inbox tab's
        query for `waiting_on_owner` (D4/D5), served by the partial index."""
        stmt = (
            select(NoteConversation)
            .where(NoteConversation.state == state)
            .order_by(NoteConversation.updated_at.desc(), NoteConversation.session_id.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars())

    async def set_state(
        self, session: AsyncSession, session_id: str, state: str
    ) -> NoteConversation | None:
        """Transition a conversation. Returns None when it is gone. An unknown state is
        refused by the DB CHECK rather than here, so Postgres stays the one authority on
        the closed set."""
        stmt = (
            update(NoteConversation)
            .where(NoteConversation.session_id == uuid.UUID(session_id))
            .values(state=state, updated_at=func.now())
            .returning(NoteConversation)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # --- the tool-call ledger --------------------------------------------------

    async def record_tool_call(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        name: str,
        args: Mapping[str, Any] | None = None,
        ok: bool,
        detail: str = "",
        entity_ids: Iterable[uuid.UUID | str] = (),
        fact_ids: Iterable[uuid.UUID | str] = (),
        domains: Sequence[str] = (),
    ) -> NoteConversationToolCall:
        """Record one call as it happens — before the assistant turn exists, hence no
        `turn_id`. `entity_ids`/`fact_ids`/`domains` are what the WRITE PATH reported,
        never what the model asked for: the ledger's job is to say what landed."""
        stmt = (
            pg_insert(NoteConversationToolCall)
            .values(
                session_id=uuid.UUID(session_id),
                name=name,
                args=cap_tool_args(args or {}),
                ok=ok,
                detail=detail,
                entity_ids=[_as_uuid(e) for e in entity_ids],
                fact_ids=[_as_uuid(f) for f in fact_ids],
                domains=list(domains),
            )
            .returning(NoteConversationToolCall)
        )
        return (await session.execute(stmt)).scalar_one()

    async def bind_turn(self, session: AsyncSession, session_id: str, turn_id: str) -> None:
        """Bind every not-yet-bound call of this conversation to the assistant turn just
        written (the `turn_attachments` idiom). Only unbound rows move, so a later
        exchange never re-attributes an earlier turn's calls."""
        await session.execute(
            update(NoteConversationToolCall)
            .where(
                NoteConversationToolCall.session_id == uuid.UUID(session_id),
                NoteConversationToolCall.turn_id.is_(None),
            )
            .values(turn_id=uuid.UUID(turn_id))
        )

    async def tool_calls(
        self, session: AsyncSession, session_id: str
    ) -> list[NoteConversationToolCall]:
        """The conversation's ledger in call order — what the D3 chip renders."""
        stmt = (
            select(NoteConversationToolCall)
            .where(NoteConversationToolCall.session_id == uuid.UUID(session_id))
            .order_by(NoteConversationToolCall.seq)
        )
        return list((await session.execute(stmt)).scalars())

    async def writes(self, session: AsyncSession, session_id: str) -> ConversationWrites:
        """The accumulated `touched`/`projected` sets for `settle_note`. FAILED calls are
        excluded: a call that errored asserted nothing, and counting its ids would spare
        a fact the whole-note sweep is supposed to retract."""
        stmt = select(
            NoteConversationToolCall.fact_ids,
            NoteConversationToolCall.entity_ids,
            NoteConversationToolCall.domains,
        ).where(
            NoteConversationToolCall.session_id == uuid.UUID(session_id),
            NoteConversationToolCall.ok.is_(True),
        )
        out = ConversationWrites()
        for facts, entities, domains in (await session.execute(stmt)).all():
            out.facts.update(facts or ())
            out.entities.update(entities or ())
            out.domains.update(domains or ())
        return out


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
