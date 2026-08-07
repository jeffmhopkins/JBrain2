"""Plan auto-continuation (docs/plans/JERV_PLANNING_TOOL_PLAN.md).

A long owner-approved plan can't finish in one turn (the per-turn step/cost/wall-clock
guardrails). So execution is chunked across turns: when a jerv turn ends while its plan
is `in_work` with unchecked `- [ ]` steps left, a continuation is scheduled ~60s out —
an owner-interruptible window. If the owner sends anything in that window the timer is
cancelled (their message supersedes and resets the cap); otherwise the sweep fires one
FRESH turn — a fresh guardrail budget — that runs the next step and re-arms. A step fired
while the owner is watching it stream (a recent foreground live-run poll) runs SUPERVISED —
the lifted per-turn budget — so a long step isn't cut off mid-work while they monitor and
can Stop; a step that fires with no client present keeps the ordinary bounded budget.

This composes existing machinery rather than adding a loop: the delay is a due-time on
the plan row + a periodic sweep (restart-safe, like the tasks scheduler); the turn runs
through `LoopTurnExecutor` (the same engine /chat and tasks use) and is recorded
answer-only (`record_answer`, no fake owner bubble), the same shape as the deferred-tool
auto-resume. It also STREAMS: the turn registers a real `_LiveTurn` broker (§Feature-1),
so a foreground/reloaded client reattaches and watches it token-by-token — the sweep turn
is server-driven, not invisible. It stays inside the "no unbounded autonomous loop"
invariant: each hop is a discrete, separately-recorded, step-capped turn, and the chain
terminates when the
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
from jbrain.agent.live_turn import _LiveTurn
from jbrain.agent.plantools import format_plan_results
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.transcript_accumulator import TranscriptAccumulator
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
# When a due step can't run yet because the session is busy (or the box is at the global
# concurrency cap), re-arm it to retry PROMPTLY on the next sweep — NOT the full 60s owner
# window. The plan isn't pausing for the owner, it's just waiting for the session to free (the
# common case: the owner approved while the draft turn was still finishing), so it should
# resume the instant it can rather than idling a minute.
BUSY_RETRY_DELAY_S = 0

# The statuses a continuation may fire on: `approved` (the owner just signed off — this is
# the FIRST step, kicked off by the approve endpoint arming a continuation) and `in_work`
# (jerv has started; it flips the status itself as it executes). Both count so approval
# alone starts the plan without the owner having to send another message.
_ACTIVE_STATUSES = ("approved", "in_work")

# How long a session's "a turn is starting" marker (set by /chat before it registers in
# live_turns) is trusted. Longer than any real turn setup, so a continuation yields to an
# owner turn mid-startup; short enough that a marker leaked by a failed setup clears fast.
TURN_STARTING_TTL_S = 30.0

# How recently a foreground PWA client must have polled this session's live-run for the
# continuation to count as SUPERVISED (→ the lifted per-turn budget, loop.guardrails_for_effort).
# The client polls live-run every ~4s while a plan works and the tab is visible; if the app is
# backgrounded or closed the poll stops and presence ages out within this window, so the next
# step falls back to the ordinary bounded budget. A generous multiple of the ~4s poll absorbs a
# couple of missed ticks while staying well under the 60s continuation window.
PRESENCE_TTL_S = 15.0

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
        plan = await PlanRepo().get_active(session, session_id)
        if plan is None or plan.status not in _ACTIVE_STATUSES or plan.awaiting_owner:
            return
        if plan.continuations_used >= MAX_CONTINUATIONS or not has_open_checklist_item(plan.body):
            return
        await PlanRepo().schedule_continuation(
            session, str(plan.plan_id), delay_s=CONTINUATION_DELAY_S
        )


@dataclass
class PlanContinuationRunner:
    """Fires due plan continuations as jerv turns that STREAM live: each registers a real
    `_LiveTurn` in the shared registry, so a foreground/reloaded client reattaches and
    watches the step's thinking + tool calls token-by-token (like an owner turn), while the
    turn is still persisted answer-only via `record_answer`. Reuses the /chat process's
    engine (`executor`), run-log, transcript, and the live-turns registry (so it never
    stacks on a live turn)."""

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
    # session_id → monotonic ts of the last foreground live-run poll (recorded by /chat's
    # `session_live_run` endpoint). A step fired while a client is present (within
    # PRESENCE_TTL_S) runs SUPERVISED — the lifted per-turn budget — because the owner is
    # watching it stream and can Stop it; otherwise it keeps the ordinary bounded budget.
    client_presence: dict[str, float] = field(default_factory=dict)
    # The global in-flight-turn cap (/chat's _MAX_CONCURRENT_TURNS), so a continuation never
    # pushes the box past it.
    max_concurrent: int = 4
    notify: NotifyBus | None = None
    push: PushPoke | None = None
    push_tokens: Callable[[], Awaitable[list[str]]] | None = field(default=None)
    # Persists the continuation turn's context fill onto the AgentSession row, so the PWA's
    # context meter restores to the true value when the chat is reopened after plan work (the
    # live stream already carries the UsageEvents; this is the reopen seed). None → skip.
    sessions: AgentSessionRepo | None = None
    # Live references to in-flight on-demand kick tasks (schedule_kick), so a fire-and-forget
    # kick isn't garbage-collected before it finishes.
    _kick_tasks: set[asyncio.Task] = field(default_factory=set)

    async def tick(self) -> None:
        """One sweep: claim every due continuation and run each."""
        raw = await self.owner_principal_id()
        if not raw:
            return
        # The resolver returns a raw uuid; the RLS GUC (set_config) binds principal_id as
        # TEXT, so it MUST be a str — a uuid throws and the whole sweep silently no-ops.
        pid = str(raw)
        owner_ctx = SessionContext(principal_id=pid, principal_kind="owner", owner_scoped=True)
        async with scoped_session(self.maker, owner_ctx) as session:
            due = await PlanRepo().claim_due_continuations(
                session, max_continuations=MAX_CONTINUATIONS
            )
        for plan_id, sid in due:
            await self._run_one(plan_id, sid, pid, owner_ctx)

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

    def _client_present(self, session_id: str) -> bool:
        """Whether a foreground PWA client is watching this session's plan — a live-run poll
        within PRESENCE_TTL_S. Gates the SUPERVISED (lifted-budget) path for the step turn."""
        seen = self.client_presence.get(session_id)
        return seen is not None and (time.monotonic() - seen) < PRESENCE_TTL_S

    async def _rearm(self, plan_id: str, owner_ctx: SessionContext) -> None:
        # A busy/cap retry, NOT the owner window — re-arm due-now so the next sweep retries the
        # instant the session frees, rather than idling the full CONTINUATION_DELAY_S.
        with contextlib.suppress(Exception):
            async with scoped_session(self.maker, owner_ctx) as s:
                await PlanRepo().schedule_continuation(s, plan_id, delay_s=BUSY_RETRY_DELAY_S)

    def schedule_kick(self) -> None:
        """Fire an on-demand sweep NOW (the owner just approved / hit Continue) so the due step
        starts without waiting for the next periodic tick — the difference between "starts
        immediately" and "starts within a sweep interval". Fire-and-forget; a task reference is
        held so it isn't GC'd mid-run. The atomic claim keeps it from double-running with the
        periodic sweep."""
        task = asyncio.create_task(self._kick())
        self._kick_tasks.add(task)
        task.add_done_callback(self._kick_tasks.discard)

    async def _kick(self) -> None:
        try:
            await self.tick()
        except Exception as exc:  # noqa: BLE001 — one bad kick must not crash the caller's task
            log.warning("plan.continuation_kick_failed", error=repr(exc))

    async def _run_one(self, plan_id: str, sid: str, pid: str, owner_ctx: SessionContext) -> None:
        # Atomic guard-and-reserve: the busy/cap checks and the live-turns registration run
        # with NO await between them, so no owner /chat turn or sibling sweep can slip in and
        # start a second turn for this session. A busy session (or a full box) re-arms and
        # retries next sweep.
        if self._session_busy(sid) or self._at_global_cap():
            await self._rearm(plan_id, owner_ctx)
            return
        # Reserve the single-turn slot with a REAL `_LiveTurn` (the same frame-buffer +
        # fan-out broker /chat uses), so a reattaching client can stream this turn live —
        # its thinking and tool calls token-by-token, exactly like an owner turn. It also
        # satisfies the concurrency guard's `.session_id`/`.done` shape and the shutdown
        # drain's `.cancel()` (task is None → a harmless no-op). Keyed by a temp id until
        # `runlog.start` yields the run_id, then re-keyed to run_id so the reattach endpoints
        # (session_live_run / resume_chat_run, keyed by run_id) discover and follow it.
        live = _LiveTurn(session_id=sid)
        active_key = uuid.uuid4().hex
        self.live_turns[active_key] = live
        try:
            # Re-read under the reservation — the plan may have changed between claim and now
            # (owner approved-away, finished the checklist, or set await_owner). Also confirm
            # this plan is STILL the session's ACTIVE plan: if a newer plan was created since the
            # claim, this one was superseded and must NOT run (its tools would resolve to the
            # newer active plan — cross-plan work).
            async with scoped_session(self.maker, owner_ctx) as s:
                plan = await PlanRepo().get_by_id(s, plan_id)
                active_id = await PlanRepo().active_plan_id(s, sid)
            if (
                plan is None
                or active_id != plan_id
                or plan.status not in _ACTIVE_STATUSES
                or plan.awaiting_owner
                or not has_open_checklist_item(plan.body)
            ):
                return
            # Count the fire now — a real turn is about to run. A claim that was skipped/
            # aborted above never reaches here, so a collision never burns a continuation.
            async with scoped_session(self.maker, owner_ctx) as s:
                await PlanRepo().bump_continuation(s, plan_id)

            profile = agent_for("jerv")
            read_ctx = read_context(pid, ())
            run_id = await self.runlog.start(
                owner_ctx, session_id=sid, prompt_version=profile.version
            )
            # Re-key the reservation to the run_id so a reattaching client that discovers this
            # session's live run (GET /chat/sessions/{id}/live-run) can then stream it
            # (GET /chat/runs/{run_id}/stream) — both look the turn up by run_id.
            self.live_turns[run_id] = self.live_turns.pop(active_key)
            active_key = run_id
            # The turn's render accumulator, exposed on the broker so the reattach snapshot
            # can seed a reloaded client from the render-so-far; the sink emits each event as
            # a `data:` SSE frame — byte-identical to the /chat drive loop — onto the broker.
            acc = TranscriptAccumulator()
            live.acc = acc
            # A step the owner is actively watching (a recent live-run poll) runs SUPERVISED —
            # the lifted per-turn budget — so a long step isn't cut off with "hit the budget"
            # while they monitor it and can Stop. Fired with no client present, it keeps the
            # ordinary bounded budget (JERV_PLANNING_TOOL_PLAN.md).
            supervised = self._client_present(sid)
            status, stop_reason, steps, cost = "error", "error", 0, 0
            try:
                executed = await self.executor.run_turn(
                    profile=profile,
                    read_ctx=read_ctx,
                    read_scopes=(),
                    conversation=_continuation_conversation(plan.body, plan.results),
                    timezone=None,
                    recorder=self.runlog.bound(owner_ctx, run_id),
                    agent_session_id=sid,
                    acc=acc,
                    on_event=lambda ev: live.emit(f"data: {ev.model_dump_json()}\n\n".encode()),
                    supervised=supervised,
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
                # Persist the meter seed so a client that reopens the chat AFTER the continuation
                # (its live stream gone) still sees the true context fill, not a stale foreground
                # value. Best-effort — the transcript is the source of truth, not this.
                used = getattr(executed, "context_used", 0)
                window = getattr(executed, "context_window", 0)
                if self.sessions is not None and used and window:
                    with contextlib.suppress(Exception):
                        await self.sessions.record_context(owner_ctx, sid, used, window)
            except Exception as exc:  # noqa: BLE001 — a continuation failure is a recorded run
                log.warning("plan.continuation_failed", session_id=sid, error=repr(exc))
            finally:
                # End every live subscriber (done or error) so a reattached client's stream
                # closes cleanly instead of hanging on the heartbeat until the row is popped.
                live.finish()
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
            live.done = True
            self.live_turns.pop(active_key, None)

    async def _nudge(self) -> None:
        """Best-effort, content-free wake so a backgrounded PWA fetches the new turn."""
        if self.push is None or self.push_tokens is None:
            return
        with contextlib.suppress(Exception):
            tokens = await self.push_tokens()
            if tokens:
                await self.push.poke(tokens)


def _continuation_conversation(body: str, results: list | None = None) -> list[UserMessage]:
    """The minimal conversation for a continuation turn: the ambient clock, the approved
    plan + its step-results scratchpad as the operating block (the checklist and the recorded
    results are the durable resumable state), and a system-event seed telling jerv to run the
    next step, record its result, and stop. Framed as data, never owner instruction."""
    plan_block = (
        "(System note — data, not owner input. The owner ALREADY APPROVED this plan for the"
        " conversation; it is your operating plan. Do the ONE next unchecked step, then record"
        " its synthesized result with write_plan_result(note=…) — that call checks the step off"
        " and advances the plan for you, so you do NOT also rewrite the checklist. REUSE the"
        " recorded results below instead of re-gathering what an earlier step already found. If"
        " you genuinely need the owner before you can proceed, call"
        ' write_plan(pause="await_owner"); otherwise just do the work.)\n\nCurrent plan:\n' + body
    )
    scratch = format_plan_results(results)
    if scratch:
        plan_block += f"\n\n--- Step results recorded so far ---\n\n{scratch}"
    seed = (
        "(System event — not owner input. You are auto-continuing your approved, in-work plan."
        " The plan is ALREADY approved, so do NOT ask the owner for permission or confirmation."
        " Actually DO the ONE next unchecked step now and produce its real output, then call"
        " write_plan_result(note=…) with that step's full synthesis (this checks the box off and"
        " advances the plan; the detailed findings live in that recorded result, not the chat)."
        " For a NON-final step, your VISIBLE reply this turn must be ONE short sentence confirming"
        " what you just finished — a specific recap of THIS step's result, e.g. 'Step 1 done —"
        " compared 5 carry-ons on capacity and warranty; front-runners are the Cotopaxi Allpa 35L,"
        " Peak Design 45L, and Tortuga Travel.' It must name the real result, NOT a generic"
        " 'working on it' / 'next up…' line and NOT nothing at all (an empty turn reads as a"
        " stall) — but keep the full findings in the recorded result, not the sentence. Then END"
        " YOUR TURN — do exactly one step per turn so the owner gets a moment between steps. When"
        " the step you are doing is the plan's FINAL step (the last unchecked item — typically"
        " 'write the guide/report/summary/answer'), you MUST instead WRITE THAT DELIVERABLE IN"
        " FULL as your visible reply THIS TURN — the actual finished content, built from the"
        " scratchpad above — NOT a one-line recap, NOT a note that it is 'ready to be written',"
        " and NOT a question asking whether to proceed, then record it and end your turn.)"
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
