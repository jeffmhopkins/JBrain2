"""Persisting the agent run log: one `agent_runs` row per turn-loop execution and
an `agent_steps` row per step.

The loop takes a `RunRecorder` (loop.py) that only knows how to record a `step`.
`AgentRunLog` owns the run lifecycle (start/finish) and the SQL; `bound()` hands
the loop a recorder pinned to one run + context, so the loop stays database-free
and the caller owns the run's start and finish (P4.5 wires this into /chat).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import AgentRun, AgentStep


class AgentRunLog:
    """CRUD for the run log, on owner-scoped sessions (runs are owner-only)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def start(self, ctx: SessionContext, *, session_id: str, prompt_version: str) -> str:
        async with scoped_session(self._maker, ctx) as session:
            run = AgentRun(session_id=uuid.UUID(session_id), prompt_version=prompt_version)
            session.add(run)
            await session.flush()
            return str(run.id)

    async def step(
        self,
        ctx: SessionContext,
        run_id: str,
        *,
        idx: int,
        kind: str,
        name: str,
        ok: bool,
        cost_tokens: int,
        tool_version: int | None = None,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            session.add(
                AgentStep(
                    run_id=uuid.UUID(run_id),
                    idx=idx,
                    kind=kind,
                    name=name,
                    tool_version=tool_version,
                    ok=ok,
                    cost_tokens=cost_tokens,
                )
            )

    async def finish(
        self,
        ctx: SessionContext,
        run_id: str,
        *,
        status: str,
        stop_reason: str,
        step_count: int,
        cost_tokens: int,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == uuid.UUID(run_id))
                .values(
                    status=status,
                    stop_reason=stop_reason,
                    step_count=step_count,
                    cost_tokens=cost_tokens,
                    ended_at=datetime.now(UTC),
                )
            )

    def bound(self, ctx: SessionContext, run_id: str) -> "BoundRecorder":
        """A `RunRecorder` (loop.py) pinned to one run and context."""
        return BoundRecorder(self, ctx, run_id)


@dataclass(frozen=True)
class BoundRecorder:
    """Adapts AgentRunLog to the loop's RunRecorder protocol: forwards each
    `step` to the bound run + context."""

    log: AgentRunLog
    ctx: SessionContext
    run_id: str

    async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None:
        await self.log.step(
            self.ctx, self.run_id, idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens
        )
