"""Plan auto-continuation (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

A long owner-approved plan can't finish in one turn (the per-turn step/cost/wall-clock
guardrails). So execution is chunked across turns: when a jerv turn ends while its plan
is `in_work` with unchecked `- [ ]` steps left, a continuation is scheduled ~60s out —
an owner-interruptible window. If the owner sends anything in that window the timer is
cancelled (their message supersedes and resets the cap); otherwise the sweep fires one
FRESH headless turn — a fresh guardrail budget — that runs the next step and re-arms.

This composes existing machinery rather than adding a loop: the delay is a due-time on
the plan row + a periodic sweep (restart-safe, like the tasks scheduler); the turn runs
through `LoopTurnExecutor` (the same engine /chat and tasks use) and is recorded
answer-only (`record_answer`, no fake owner bubble), the same shape as the deferred-tool
auto-resume. It stays inside the "no unbounded autonomous loop" invariant: each hop is a
discrete, separately-recorded, step-capped turn, and the chain terminates when the
checklist is done, the status leaves `in_work`, jerv signals `await_owner`, the owner
sends anything, or a max-continuations cap is hit.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from jbrain.agent.agents import agent_for
from jbrain.agent.clock import now_block
from jbrain.agent.session import read_context
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import UserMessage
from jbrain.models.plan import PlanRepo, has_open_checklist_item

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.agent.runlog import AgentRunLog
    from jbrain.agent.transcript_store import AgentTranscript
    from jbrain.notify import NotifyBus
    from jbrain.tasks.runner import LoopTurnExecutor, PushPoke

log = structlog.get_logger()

# The owner-interruptible window before a continuation fires, the sweep cadence, and the
# hard cap on how many continuations one approved plan may auto-fire before it must wait
# for the owner again (the human-anchored bound; the owner sending anything resets it).
CONTINUATION_DELAY_S = 60
SWEEP_INTERVAL_S = 15
MAX_CONTINUATIONS = 20

# Turn outcomes that must NOT schedule a continuation: a deferred turn already handed off
# to a background job (its own resume continues the chat), and an errored/cancelled/
# stranded turn should not spin the loop.
_NO_CONTINUE_STOPS = frozenset({"deferred", "error", "stranded", "cancelled", "canceled"})


async def maybe_schedule_continuation(
    maker: async_sessionmaker[AsyncSession],
    owner_ctx: SessionContext,
    session_id: str,
    *,
    agent: str,
    stop_reason: str,
) -> None:
    """The shared settle decision, called after any jerv turn (the live /chat turn and a
    continuation turn alike): arm a continuation if this chat's plan is in-work with
    unchecked steps, not awaiting the owner, and under the cap. Best-effort — the caller
    wraps it so a hiccup never breaks the turn."""
    if agent != "jerv" or stop_reason in _NO_CONTINUE_STOPS:
        return
    async with scoped_session(maker, owner_ctx) as session:
        plan = await PlanRepo().get(session, session_id)
        if plan is None or plan.status != "in_work" or plan.awaiting_owner:
            return
        if plan.continuations_used >= MAX_CONTINUATIONS or not has_open_checklist_item(plan.body):
            return
        await PlanRepo().schedule_continuation(session, session_id, delay_s=CONTINUATION_DELAY_S)


class _ContinuationTurn:
    """A live-turn marker the /chat concurrency guard reads (it checks `session_id` +
    `done`), so a continuation turn occupies the same single-turn-per-session slot and an
    owner turn can't stack on top of it."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.done = False


@dataclass
class PlanContinuationRunner:
    """Fires due plan continuations as headless, answer-only jerv turns. Reuses the
    /chat process's engine (`executor`), run-log, transcript, and the live-turns registry
    (so it never stacks on a live turn)."""

    maker: async_sessionmaker[AsyncSession]
    executor: LoopTurnExecutor
    runlog: AgentRunLog
    transcript: AgentTranscript
    live_turns: dict[str, object]
    owner_principal_id: Callable[[], Awaitable[str | None]]
    notify: NotifyBus | None = None
    push: PushPoke | None = None
    push_tokens: Callable[[], Awaitable[list[str]]] | None = field(default=None)

    async def tick(self) -> None:
        """One sweep: claim every due continuation and run each."""
        pid = await self.owner_principal_id()
        if not pid:
            return
        owner_ctx = SessionContext(
            principal_id=pid, principal_kind="owner", owner_scoped=True
        )
        async with scoped_session(self.maker, owner_ctx) as session:
            due = await PlanRepo().claim_due_continuations(
                session, max_continuations=MAX_CONTINUATIONS
            )
        for sid in due:
            await self._run_one(sid, pid, owner_ctx)

    async def _session_is_live(self, session_id: str) -> bool:
        return any(
            getattr(lt, "session_id", None) == session_id and not getattr(lt, "done", True)
            for lt in self.live_turns.values()
        )

    async def _run_one(self, sid: str, pid: str, owner_ctx: SessionContext) -> None:
        # Never stack on a live turn (an owner turn, or another continuation): re-arm and
        # let the next sweep retry once the session is free.
        if await self._session_is_live(sid):
            with contextlib.suppress(Exception):
                async with scoped_session(self.maker, owner_ctx) as s:
                    await PlanRepo().schedule_continuation(s, sid, delay_s=CONTINUATION_DELAY_S)
            return

        # Re-read under the claim — the plan may have changed between claim and run (owner
        # approved-away, hit the checklist end, or set await_owner).
        async with scoped_session(self.maker, owner_ctx) as s:
            plan = await PlanRepo().get(s, sid)
        if (
            plan is None
            or plan.status != "in_work"
            or plan.awaiting_owner
            or not has_open_checklist_item(plan.body)
        ):
            return

        profile = agent_for("jerv")
        read_ctx = read_context(pid, ())
        run_id = await self.runlog.start(owner_ctx, session_id=sid, prompt_version=profile.version)
        marker = _ContinuationTurn(sid)
        self.live_turns[run_id] = marker

        status, stop_reason, steps, cost = "error", "error", 0, 0
        try:
            executed = await self.executor.run_turn(
                profile=profile,
                read_ctx=read_ctx,
                read_scopes=(),
                conversation=_continuation_conversation(plan.body),
                timezone=None,
                recorder=self.runlog.bound(owner_ctx, run_id),
                agent_session_id=sid,
            )
            status, stop_reason = "done", executed.result.stop_reason
            steps, cost = executed.result.steps, executed.result.cost_tokens
            with contextlib.suppress(Exception):
                await self.transcript.record_answer(
                    owner_ctx,
                    session_id=sid,
                    run_id=run_id,
                    assistant_text=executed.result.text,
                    tools=executed.tools,
                    reasoning=executed.reasoning,
                )
        except Exception as exc:  # noqa: BLE001 — a continuation failure is a recorded run
            log.warning("plan.continuation_failed", session_id=sid, error=repr(exc))
        finally:
            marker.done = True
            self.live_turns.pop(run_id, None)

        with contextlib.suppress(Exception):
            await self.runlog.finish(
                owner_ctx,
                run_id,
                status=status,
                stop_reason=stop_reason,
                step_count=steps,
                cost_tokens=cost,
            )
        if status == "done":
            # Re-run the settle decision so the loop advances to the next step (this turn
            # never went through /chat's settle hook), and nudge the owner it moved.
            with contextlib.suppress(Exception):
                await maybe_schedule_continuation(
                    self.maker, owner_ctx, sid, agent="jerv", stop_reason=stop_reason
                )
            await self._nudge()

    async def _nudge(self) -> None:
        """Best-effort, content-free wake so a backgrounded PWA fetches the new turn."""
        if self.push is None or self.push_tokens is None:
            return
        with contextlib.suppress(Exception):
            tokens = await self.push_tokens()
            if tokens:
                await self.push.poke(tokens)


def _continuation_conversation(body: str) -> list[UserMessage]:
    """The minimal conversation for a continuation turn: the ambient clock, the approved
    plan as the operating block (its checklist is the durable resumable state), and a
    system-event seed telling jerv to run the next step and stop. Framed as data, never
    owner instruction."""
    plan_block = (
        "(System note — data, not owner input. The owner APPROVED this plan for the"
        " conversation; it is your operating plan. Execute the NEXT unchecked step, mark it"
        " done with write_plan(body=…), then STOP — do not redo completed steps. If you need"
        ' the owner before you can proceed, call write_plan(pause="await_owner") instead of'
        " guessing.)\n\nCurrent plan:\n" + body
    )
    seed = (
        "(System event — not owner input. You are auto-continuing your approved, in-work plan"
        " for this conversation. Do the next unchecked step now, update the checklist, then"
        " end your turn.)"
    )
    return [UserMessage(text=now_block(None)), UserMessage(text=plan_block), UserMessage(text=seed)]


async def run_plan_continuation_loop(
    runner: PlanContinuationRunner, *, interval_s: int = SWEEP_INTERVAL_S
) -> None:
    """The web-process sweep driver: tick the runner every `interval_s`. Fault-swallowing
    per tick (a sweep hiccup must not kill the loop); CancelledError propagates so
    shutdown stops it cleanly."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await runner.tick()
        except Exception as exc:  # noqa: BLE001 — one bad sweep must not end the loop
            log.warning("plan.continuation_sweep_failed", error=repr(exc))
