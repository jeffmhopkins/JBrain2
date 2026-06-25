"""Executing a task: spawn an agent session under the task's persona/scope, run one
agent turn headless, and record the run + the session it produced.

This mirrors the /chat turn (api/agent.py) without the SSE plumbing: it reuses the
same building blocks — `AgentSessionRepo`, `AgentRunLog`, `AgentTranscript`, the
`agent_for` profile, and the `AgentLoop`. The actual turn is run behind a small
`TurnExecutor` protocol so the runner is unit-testable with a fake; `LoopTurnExecutor`
is the real one that drives `AgentLoop.run`. Each step is fail-closed: a turn that
raises is recorded as an `error` run, never propagated to the scheduler tick.
"""

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import structlog

from jbrain.agent.agents import AgentProfile, agent_for
from jbrain.agent.clock import now_block
from jbrain.agent.loop import AgentLoop, AgentResult, guardrails_for_effort
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.db.session import SessionContext
from jbrain.llm import LlmRouter, UserMessage
from jbrain.tasks.repo import TaskInfo, TaskRunInfo, TaskRunRepo

log = structlog.get_logger()

_SUMMARY_LEN = 240


class TurnExecutor(Protocol):
    """Runs one agent turn to completion and returns its result. The real impl drives
    `AgentLoop.run`; tests inject a fake to avoid the LLM stack."""

    async def run_turn(
        self,
        *,
        profile: AgentProfile,
        read_ctx: SessionContext,
        read_scopes: Sequence[str],
        conversation: Sequence[object],
        timezone: str | None,
        recorder: object,
        agent_session_id: str,
    ) -> AgentResult: ...


class PushPoke(Protocol):
    async def poke(self, tokens: list[str]) -> None: ...


@dataclass
class LoopTurnExecutor:
    """The production `TurnExecutor`: one ReAct turn through `AgentLoop`, sized to the
    agent.turn model's reasoning effort exactly as /chat does."""

    router: LlmRouter
    registry: ToolRegistry

    async def run_turn(
        self,
        *,
        profile: AgentProfile,
        read_ctx: SessionContext,
        read_scopes: Sequence[str],
        conversation: Sequence[object],
        timezone: str | None,
        recorder: object,
        agent_session_id: str,
    ) -> AgentResult:
        effort = await self.router.effective_reasoning_effort("agent.turn")
        loop = AgentLoop(
            self.router,
            self.registry,
            recorder=recorder,  # type: ignore[arg-type]
            guardrails=guardrails_for_effort(effort),
        )
        return await loop.run(
            session=read_ctx,
            scopes=read_scopes,
            conversation=conversation,  # type: ignore[arg-type]
            timezone=timezone,
            system=profile.prompt,
            agent_session_id=agent_session_id,
            tools_allow=profile.tools,
        )


class TaskRunner:
    def __init__(
        self,
        *,
        sessions: AgentSessionRepo,
        runlog: AgentRunLog,
        transcript: AgentTranscript,
        runs: TaskRunRepo,
        executor: TurnExecutor,
        push: PushPoke | None = None,
        push_tokens: Sequence[str] = (),
    ):
        self._sessions = sessions
        self._runlog = runlog
        self._transcript = transcript
        self._runs = runs
        self._executor = executor
        self._push = push
        self._push_tokens = list(push_tokens)

    async def run(self, owner_ctx: SessionContext, task: TaskInfo, *, trigger: str) -> TaskRunInfo:
        """Execute one run of `task`. `owner_ctx` must carry the owner's principal id
        (the session/run/transcript are owner-only). Returns the finished run record;
        never raises for an agent failure — that lands as an `error` run."""
        profile = agent_for(task.agent)
        # A non-KB agent (jerv/teacher) runs with empty read scopes: the firewall, not
        # a flag, so even a mis-scoped task touches no owner data.
        read_scopes = tuple(task.domain_scopes) if profile.reads_knowledge_base else ()

        session = await self._sessions.create(
            owner_ctx,
            domain_scopes=list(read_scopes),
            title=task.name or "Task",
            agent=task.agent,
        )
        run_id = await self._runlog.start(
            owner_ctx, session_id=session.id, prompt_version=profile.version
        )
        task_run_id = await self._runs.start(
            owner_ctx,
            task_id=task.id,
            principal_id=owner_ctx.principal_id,
            session_id=session.id,
            run_id=run_id,
            trigger=trigger,
        )
        read_ctx = read_context(owner_ctx.principal_id, read_scopes)
        # Ground the turn in the current date/time (the same data-framed message /chat
        # prepends) so "today"/"this week" resolve without a tool call.
        conversation = [UserMessage(text=now_block(task.timezone)), UserMessage(text=task.prompt)]
        recorder = self._runlog.bound(owner_ctx, run_id)

        status = "error"
        summary = ""
        error: str | None = None
        steps = 0
        cost = 0
        stop_reason = "error"
        try:
            result = await self._executor.run_turn(
                profile=profile,
                read_ctx=read_ctx,
                read_scopes=read_scopes,
                conversation=conversation,
                timezone=task.timezone,
                recorder=recorder,
                agent_session_id=session.id,
            )
            status, summary, steps, cost = "done", result.text, result.steps, result.cost_tokens
            stop_reason = result.stop_reason
            with contextlib.suppress(Exception):
                await self._transcript.record_exchange(
                    owner_ctx,
                    session_id=session.id,
                    run_id=run_id,
                    user_text=task.prompt,
                    assistant_text=result.text,
                    tools=[],
                )
        except Exception as exc:  # noqa: BLE001 — a task failure is a recorded run, not a crash
            log.warning("task.run_failed", task_id=task.id, error=repr(exc))
            error = str(exc)

        with contextlib.suppress(Exception):
            await self._runlog.finish(
                owner_ctx,
                run_id,
                status=status,
                stop_reason=stop_reason,
                step_count=steps,
                cost_tokens=cost,
            )
        with contextlib.suppress(Exception):
            await self._runs.finish(
                owner_ctx,
                task_run_id,
                status=status,
                summary=summary[:_SUMMARY_LEN],
                error=error,
                step_count=steps,
                cost_tokens=cost,
            )
        with contextlib.suppress(Exception):
            await self._sessions.touch(owner_ctx, session.id)

        # Delivery is best-effort and content-free: a push only wakes the PWA, which
        # fetches the run over its authenticated channel (no PII to the push service).
        if task.notify_push and self._push is not None and self._push_tokens:
            with contextlib.suppress(Exception):
                await self._push.poke(self._push_tokens)

        return TaskRunInfo(
            id=task_run_id,
            task_id=task.id,
            session_id=session.id,
            run_id=run_id,
            status=status,
            trigger=trigger,
            summary=summary[:_SUMMARY_LEN],
            error=error,
            step_count=steps,
            cost_tokens=cost,
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
        )
