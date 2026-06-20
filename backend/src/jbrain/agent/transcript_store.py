"""Persisting a session's conversation transcript so reopening Full Brain replays
the same exchange (docs/ASSISTANT.md "Sessions").

Owner-only, append-only: one `agent_turns` row per turn. A completed turn writes
the user message then the assistant answer (with the tool sources it surfaced) in
one transaction; `load` returns them in order for the PWA to seed the surface.
Recording is best-effort at the call site — a write failure never breaks a turn.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.attachments import AttachmentInfo, TurnAttachmentRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import AgentTurn


@dataclass(frozen=True)
class TurnRecord:
    role: str  # user | assistant
    content: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""  # the assistant turn's thinking trace (gpt-oss/GLM); "" otherwise
    # The chat files bound to a USER turn (Stage-2 Wave 2), replayed as attachment
    # chips; always empty for an assistant turn.
    attachments: list[AttachmentInfo] = field(default_factory=list)


class AgentTranscript:
    """Reads/writes the per-session transcript on owner-scoped sessions."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        attachments: TurnAttachmentRepo | None = None,
    ):
        self._maker = maker
        # Optional so existing callers/tests that predate chat attachments still
        # construct the transcript; when absent, `load` simply returns no attachments.
        self._attachments = attachments

    async def record_exchange(
        self,
        ctx: SessionContext,
        *,
        session_id: str,
        run_id: str | None,
        user_text: str,
        assistant_text: str,
        tools: Sequence[dict[str, Any]],
        reasoning: str = "",
    ) -> str:
        """Append the user turn then the assistant turn for one completed exchange.
        Returns the new USER turn's id so the caller can bind the turn's pre-uploaded
        attachments to it (the attachment row is owner-scoped on its own firewall, so
        binding runs separately under the session's narrowed context)."""
        sid = uuid.UUID(session_id)
        rid = uuid.UUID(run_id) if run_id is not None else None
        async with scoped_session(self._maker, ctx) as session:
            user_turn = AgentTurn(
                session_id=sid, run_id=rid, role="user", content=user_text, tools=[]
            )
            session.add(user_turn)
            session.add(
                AgentTurn(
                    session_id=sid,
                    run_id=rid,
                    role="assistant",
                    content=assistant_text,
                    tools=list(tools),
                    reasoning=reasoning,
                )
            )
            await session.flush()
            return str(user_turn.id)

    async def load(self, ctx: SessionContext, session_id: str) -> list[TurnRecord]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(AgentTurn)
                        .where(AgentTurn.session_id == uuid.UUID(session_id))
                        .order_by(AgentTurn.seq)
                    )
                )
                .scalars()
                .all()
            )
        # One RLS-scoped round-trip for every user turn's attachments, so a reopened
        # session replays the files on the turn that carried them.
        by_turn: dict[str, list[AttachmentInfo]] = {}
        if self._attachments is not None:
            user_turn_ids = [str(r.id) for r in rows if r.role == "user"]
            by_turn = await self._attachments.list_for_turns(ctx, user_turn_ids)
        return [
            TurnRecord(
                role=r.role,
                content=r.content,
                tools=list(r.tools),
                reasoning=r.reasoning,
                attachments=by_turn.get(str(r.id), []),
            )
            for r in rows
        ]
