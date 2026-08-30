"""The ledger engine's night: compose, sit, extract, tend.

The second engine (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2), behind `jmolt_engine=ledger`.
It is a separate class from `JmoltNightRunner` on purpose. Both engines share everything the
threat model rests on — the tools, the outbox chokepoint, the caps and rate ledger, the content
lint, the kill switch, the M19 RLS split, the observer — and share nothing else, so neither is
a migration the other has to survive and both can be run against the same recorded corpus
before either is chosen.

What differs from the shipped engine, in the order it matters:

1. **The context is composed, never appended.** Each sitting gets a brief rendered from typed
   ledger rows by `jmolt_compose`. The model never re-reads its own prose; the only text in the
   brief that a model wrote is verbatim evidence, quoted and attributed.
2. **Publishing tools are absent for most of the hour** (`jmolt_phases`), rather than
   discouraged in a prompt a 120B reads as a suggestion.
3. **Promises are extracted from what was published**, deterministically, and become
   obligations the next sitting's brief carries — whether or not the model remembers.

The loop is deliberately dull. Everything interesting is in the four modules it calls, each of
which is unit-tested on its own; a runner that also made judgements would be a runner nobody
could test.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.agents import agent_for
from jbrain.agent.jmolt_compose import OwnerNote, compose_brief
from jbrain.agent.jmolt_night import (
    JMOLT_LAST_SITTING_MARGIN_S,
    JMOLT_MAX_SITTINGS,
    JMOLT_NIGHT_WALL_CLOCK_S,
    jmolt_run_context,
)
from jbrain.agent.jmolt_phases import hidden_for, phase_for
from jbrain.agent.jmolt_promise import find_promises
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import UserMessage
from jbrain.models.jmolt_obligation import ObligationRepo
from jbrain.models.jmolt_outbox import OutboxRepo

log = structlog.get_logger(__name__)

# How long a note stays live before it stops appearing in the brief. A wish that never expires
# becomes a rule by accident; a week is long enough that a considered request survives a few
# nights of the agent not getting to it.
NOTE_LIFETIME = timedelta(days=7)


@dataclass
class SittingOutcome:
    """One sitting, as the night records it. Read from the outbox and the ledger — never from
    what the model said it did, which is the thing under study."""

    sitting: int
    phase: str
    published: int = 0
    promises_found: int = 0
    error: str = ""


class JmoltLedgerRunner:
    """Runs one night on the ledger engine."""

    def __init__(
        self,
        *,
        sessions: Any,
        runlog: Any,
        transcript: Any,
        executor: Any,
        settings_store: Any,
        maker: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] | None = None,
        max_sittings: int = JMOLT_MAX_SITTINGS,
    ) -> None:
        self._sessions = sessions
        self._runlog = runlog
        self._transcript = transcript
        self._executor = executor
        self._settings = settings_store
        self._maker = maker
        self._obligations = ObligationRepo()
        self._outbox = OutboxRepo()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_sittings = max_sittings
        self.outcomes: list[SittingOutcome] = []

    async def run(self, owner_ctx: SessionContext) -> str:
        """One night. Never raises: a night that died is a fact about the engine, and a
        scheduler that has to catch exceptions is a scheduler that stops running."""
        profile = agent_for("jmolt")
        tz = await self._settings.owner_timezone(owner_ctx) or "UTC"
        handle = await self._settings.moltbook_handle(owner_ctx) or "jmolt"
        note = await self._note(owner_ctx)
        session = await self._sessions.create(
            owner_ctx, domain_scopes=[], title="jmolt night (ledger)", agent="jmolt"
        )
        session_id = str(session.id)
        read_ctx = jmolt_run_context(owner_ctx.principal_id)
        woke_at = self._clock()
        self.outcomes = []

        for sitting in range(1, self._max_sittings + 1):
            now = self._clock()
            if (now - woke_at).total_seconds() >= (
                JMOLT_NIGHT_WALL_CLOCK_S - JMOLT_LAST_SITTING_MARGIN_S
            ):
                break
            # A kill engaged mid-night stops the next sitting, exactly as the shipped engine
            # does. Shared mechanism, not a reimplementation of the idea.
            if sitting > 1 and await self._settings.moltbook_killed(owner_ctx):
                break
            phase = phase_for(sitting, budget=self._max_sittings)
            outcome = await self._sitting(
                owner_ctx,
                read_ctx,
                profile=profile,
                session_id=session_id,
                sitting=sitting,
                phase=phase,
                tz=tz,
                handle=handle,
                note=note,
                woke_at=woke_at,
                now=now,
            )
            self.outcomes.append(outcome)
            if phase == "tending":
                break
        return session_id

    async def _sitting(
        self,
        owner_ctx: SessionContext,
        read_ctx: SessionContext,
        *,
        profile: Any,
        session_id: str,
        sitting: int,
        phase: str,
        tz: str,
        handle: str,
        note: OwnerNote | None,
        woke_at: datetime,
        now: datetime,
    ) -> SittingOutcome:
        outcome = SittingOutcome(sitting=sitting, phase=phase)
        brief = await self._brief(read_ctx, handle=handle, now=now, woke_at=woke_at, note=note)
        run_id = await self._runlog.start(
            owner_ctx, session_id=session_id, prompt_version=profile.version
        )
        recorder = self._runlog.bound(owner_ctx, run_id)
        # What the outbox already held, so "published this sitting" is a difference rather than
        # a guess. Promise extraction runs over the difference only: re-scanning the night's
        # whole output every sitting would re-open obligations closed an hour ago.
        before = {r.id for r in await self._staged(read_ctx)}
        try:
            executed = await self._executor.run_turn(
                profile=profile,
                read_ctx=read_ctx,
                read_scopes=(),
                conversation=[UserMessage(text=brief)],
                timezone=tz,
                recorder=recorder,
                agent_session_id=session_id,
                hidden=hidden_for(phase),  # type: ignore[arg-type]
            )
            result = executed.result
            with contextlib.suppress(Exception):
                await self._transcript.record_exchange(
                    owner_ctx,
                    session_id=session_id,
                    run_id=run_id,
                    user_text=brief,
                    assistant_text=result.text,
                    tools=executed.tools,
                    reasoning=executed.reasoning,
                )
            with contextlib.suppress(Exception):
                await self._runlog.finish(
                    owner_ctx,
                    run_id,
                    status="done",
                    stop_reason=result.stop_reason,
                    step_count=result.steps,
                    cost_tokens=result.cost_tokens,
                )
        except Exception as exc:  # noqa: BLE001 — a dead sitting is a data point, not a crash
            log.warning("jmolt_ledger.sitting_failed", sitting=sitting, exc_info=True)
            outcome.error = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                await self._runlog.finish(
                    owner_ctx,
                    run_id,
                    status="error",
                    stop_reason="error",
                    step_count=0,
                    cost_tokens=0,
                )

        fresh = [r for r in await self._staged(read_ctx) if r.id not in before]
        outcome.published = len(fresh)
        outcome.promises_found = await self._record_promises(read_ctx, fresh)
        return outcome

    async def _brief(
        self,
        read_ctx: SessionContext,
        *,
        handle: str,
        now: datetime,
        woke_at: datetime,
        note: OwnerNote | None,
    ) -> str:
        elapsed = (now - woke_at).total_seconds()
        minutes_left = max(0, int((JMOLT_NIGHT_WALL_CLOCK_S - elapsed) // 60))
        async with scoped_session(self._maker, read_ctx) as s:
            open_ = await self._obligations.open_(s, read_ctx.principal_id)
            closed = await self._obligations.closed_since(
                s, read_ctx.principal_id, since=now - timedelta(days=2)
            )
        return compose_brief(
            handle=handle,
            now=now,
            minutes_left=minutes_left,
            open_obligations=open_,
            closed_recently=closed,
            note=note,
        )

    async def _staged(self, read_ctx: SessionContext) -> list[Any]:
        async with scoped_session(self._maker, read_ctx) as s:
            return await self._outbox.list_by_status(
                s, read_ctx.principal_id, ("queued", "released", "published")
            )

    async def _record_promises(self, read_ctx: SessionContext, rows: list[Any]) -> int:
        """Open an obligation per promise in what this sitting published.

        Reads the OUTBOX PAYLOAD, not the model's account of what it wrote. Those differ — a
        night that narrated four posts and made one is the failure being measured — and the
        payload is what other agents will actually read."""
        found = 0
        async with scoped_session(self._maker, read_ctx) as s:
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else {}
                text = f"{payload.get('title', '')} {payload.get('content', '')}".strip()
                for promise in find_promises(text):
                    opened = await self._obligations.open(
                        s,
                        read_ctx.principal_id,
                        kind="commitment",
                        subject=promise.subject,
                        quote=promise.quote,
                        source="self",
                    )
                    if opened:
                        found += 1
        return found

    async def _note(self, owner_ctx: SessionContext) -> OwnerNote | None:
        """The owner's note, with an expiry attached.

        The stored note carries no timestamp of its own, so its age is unknowable and the
        lifetime is applied from NOW — which means a note the owner wrote a month ago and
        forgot still gets one more week. That is the wrong direction for the "notes expire"
        property and it is a gap, not a design: closing it needs a written_at column on the
        setting, which is a schema change this wave does not make. Recorded here rather than
        in a plan nobody reads at the call site."""
        raw = await self._settings.moltbook_advisory_note(owner_ctx)
        if not raw or not raw.strip():
            return None
        now = self._clock()
        return OwnerNote(text=raw, written_at=now, expires_at=now + NOTE_LIFETIME)
