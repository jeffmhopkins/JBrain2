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

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import AgentTurn


@dataclass(frozen=True)
class TurnRecord:
    role: str  # user | assistant
    content: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""  # the assistant turn's thinking trace (gpt-oss/GLM); "" otherwise


class AgentTranscript:
    """Reads/writes the per-session transcript on owner-scoped sessions."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

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
    ) -> None:
        """Append the user turn then the assistant turn for one completed exchange."""
        sid = uuid.UUID(session_id)
        rid = uuid.UUID(run_id) if run_id is not None else None
        async with scoped_session(self._maker, ctx) as session:
            session.add(
                AgentTurn(session_id=sid, run_id=rid, role="user", content=user_text, tools=[])
            )
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

    async def load(self, ctx: SessionContext, session_id: str) -> list[TurnRecord]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(AgentTurn)
                    .where(AgentTurn.session_id == uuid.UUID(session_id))
                    .order_by(AgentTurn.seq)
                )
            ).scalars()
            return [
                TurnRecord(
                    role=r.role, content=r.content, tools=list(r.tools), reasoning=r.reasoning
                )
                for r in rows
            ]
