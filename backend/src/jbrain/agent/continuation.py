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
import time
import uuid
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

# The statuses a continuation may fire on: `approved` (the owner just signed off — this is
# the FIRST step, kicked off by the approve endpoint arming a continuation) and `in_work`
# (jerv has started; it flips the status itself as it executes). Both count so approval
# alone starts the plan without the owner having to send another message.
_ACTIVE_STATUSES = ("approved", "in_work")

# How long a session's "a turn is starting" marker (set by /chat before it registers in
# live_turns) is trusted. Longer than any real turn setup, so a continuation yields to an
# owner turn mid-startup; short enough that a marker leaked by a failed setup clears fast.
TURN_STARTING_TTL_S = 30.0

# Turn outcomes that must NOT schedule a continuation: a deferred turn already handed off
# to a background job (its own resume continues the chat); an errored/cancelled/stranded
# turn should not spin the loop; and `too_many_errors` is a persistent-failure stop —
# re-arming it would just burn continuations retrying a step jerv can't complete (the cap
# would bound it, but blocking it here avoids the waste).
_NO_CONTINUE_STOPS = frozenset(
    {"deferred", "error", "stranded", "cancelled", "canceled", "too_many_errors"}
)


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
        if plan is None or plan.status not in _ACTIVE_STATUSES or plan.awaiting_owner:
            return
        if plan.continuations_used >= MAX_CONTINUATIONS or not has_open_checklist_item(plan.body):
            return
        await PlanRepo().schedule_continuation(session, session_id, delay_s=CONTINUATION_DELAY_S)


class _ContinuationTurn:
    """A live-turn marker the /chat concurrency guard reads (it checks `session_id` +
    `done`), so a continuation turn occupies the same single-turn-per-session slot and an
    owner turn can't stack on top of it. `task`/`cancel()` mirror `_LiveTurn`'s shape so
    the lifespan shutdown drain — which calls `.cancel()` on every live-turns value — treats
    a continuation marker harmlessly (a continuation turn is awaited by the sweep, not the
    shutdown; there is no task to cancel)."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.done = False
        self.task = None

    def cancel(self) -> None:
        return None


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
    # /chat records "a turn is starting for this session" here (session_id → monotonic ts)
    # the instant it passes its concurrency guard — before it has registered in live_turns.
    # Reading it lets a continuation yield to an owner turn still mid-startup, closing the
    # window where both could run the same step (JERV_PLANNING_TOOL_PLAN.md).
    turn_starting: dict[str, float] = field(default_factory=dict)
    # The global in-flight-turn cap (/chat's _MAX_CONCURRENT_TURNS), so a continuation never
    # pushes the box past it.
    max_concurrent: int = 4
    notify: NotifyBus | None = None
    push: PushPoke | None = None
    push_tokens: Callable[[], Awaitable[list[str]]] | None = field(default=None)

    async def tick(self) -> None:
        """One sweep: claim every due continuation and run each."""
        pid = await self.owner_principal_id()
        if not pid:
            return
        owner_ctx = SessionContext(principal_id=pid, principal_kind="owner", owner_scoped=True)
        async with scoped_session(self.maker, owner_ctx) as session:
            due = await PlanRepo().claim_due_continuations(
                session, max_continuations=MAX_CONTINUATIONS
            )
        for sid in due:
            await self._run_one(sid, pid, owner_ctx)

    def _session_busy(self, session_id: str) -> bool:
        """Whether an owner turn (live, or mid-startup within the TTL) holds this session.
        SYNCHRONOUS on purpose: the caller pairs this check with the marker registration
        under no `await`, so the check-and-reserve is atomic against every other coroutine."""
        for lt in self.live_turns.values():
            if getattr(lt, "session_id", None) == session_id and not getattr(lt, "done", True):
                return True
        started = self.turn_starting.get(session_id)
        return started is not None and (time.monotonic() - started) < TURN_STARTING_TTL_S

    def _at_global_cap(self) -> bool:
        live = sum(1 for lt in self.live_turns.values() if not getattr(lt, "done", True))
        return live >= self.max_concurrent

    async def _rearm(self, sid: str, owner_ctx: SessionContext) -> None:
        with contextlib.suppress(Exception):
            async with scoped_session(self.maker, owner_ctx) as s:
                await PlanRepo().schedule_continuation(s, sid, delay_s=CONTINUATION_DELAY_S)

    async def _run_one(self, sid: str, pid: str, owner_ctx: SessionContext) -> None:
        # Atomic guard-and-reserve: the busy/cap checks and the live-turns registration run
        # with NO await between them, so no owner /chat turn or sibling sweep can slip in and
        # start a second turn for this session. A busy session (or a full box) re-arms and
        # retries next sweep.
        if self._session_busy(sid) or self._at_global_cap():
            await self._rearm(sid, owner_ctx)
            return
        key = uuid.uuid4().hex
        marker = _ContinuationTurn(sid)
        self.live_turns[key] = marker
        try:
            # Re-read under the reservation — the plan may have changed between claim and now
            # (owner approved-away, finished the checklist, or set await_owner).
            async with scoped_session(self.maker, owner_ctx) as s:
                plan = await PlanRepo().get(s, sid)
            if (
                plan is None
                or plan.status not in _ACTIVE_STATUSES
                or plan.awaiting_owner
                or not has_open_checklist_item(plan.body)
            ):
                return
            # Count the fire now — a real turn is about to run. A claim that was skipped/
            # aborted above never reaches here, so a collision never burns a continuation.
            async with scoped_session(self.maker, owner_ctx) as s:
                await PlanRepo().bump_continuation(s, sid)

            profile = agent_for("jerv")
            read_ctx = read_context(pid, ())
            run_id = await self.runlog.start(
                owner_ctx, session_id=sid, prompt_version=profile.version
            )
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
        finally:
            marker.done = True
            self.live_turns.pop(key, None)

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
