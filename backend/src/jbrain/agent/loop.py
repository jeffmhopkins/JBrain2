"""The agent turn loop: a thin ReAct cycle over the LLM adapter.

Assemble the conversation, ask the model with the in-scope tools, run any tool
calls it makes, feed the results back, and repeat until it answers or a guardrail
trips. The loop owns the guardrails — step, cost, and consecutive-error caps —
and never trusts the model to stop itself. Tool dispatch and the run record are
the loop's concern; what a tool *does* is the handler's.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from jbrain.agent.contracts import (
    ChatEvent,
    DoneEvent,
    EntityRef,
    NoteSource,
    ProposalRef,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
    ToolViewEvent,
    ViewPayload,
)
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.llm import (
    AssistantMessage,
    LlmMessage,
    LlmRouter,
    LlmTurn,
    TextChunk,
    ToolCall,
    ToolResult,
    ToolResultMessage,
)
from jbrain.llm.promptfile import load_prompt

log = structlog.get_logger()

_SYSTEM = load_prompt(Path(__file__).parent / "prompts" / "system.prompt")
SYSTEM_PROMPT: str = _SYSTEM.render()
SYSTEM_VERSION: str = _SYSTEM.version
SYSTEM_STRENGTH: str = _SYSTEM.strength


@dataclass(frozen=True)
class Guardrails:
    """Hard limits the loop enforces, never the model. A run that hits one stops
    with the corresponding stop reason rather than spinning or overspending."""

    max_steps: int = 10
    max_cost_tokens: int = 200_000
    max_consecutive_tool_errors: int = 3


@dataclass(frozen=True)
class ToolContext:
    """What a tool handler receives: the RLS scope its reads must run under."""

    session: SessionContext
    scopes: tuple[str, ...]


class ToolOutput(str):
    """A tool observation that also carries what the tool surfaced for the UI —
    note sources (source cards), a staged proposal (a "Review proposal" chip),
    resolved entities, and/or a rich `view` (a registered component the PWA
    renders, e.g. a checklist). It *is* the model-facing text (a str subclass), so
    handlers keep their `-> str` contract and existing call sites are untouched;
    `_dispatch` pulls the extras off when present."""

    sources: tuple[NoteSource, ...]
    proposal: ProposalRef | None
    entities: tuple[EntityRef, ...]
    view: ViewPayload | None

    def __new__(
        cls,
        content: str,
        sources: tuple[NoteSource, ...] = (),
        proposal: ProposalRef | None = None,
        entities: tuple[EntityRef, ...] = (),
        view: ViewPayload | None = None,
    ) -> "ToolOutput":
        out = super().__new__(cls, content)
        out.sources = sources
        out.proposal = proposal
        out.entities = entities
        out.view = view
        return out


# A tool handler runs one call and returns the observation text fed back to the
# model (a ToolOutput when it also has sources to surface). Raising marks the call
# an error (an observation the model can recover from), never a crash.
ToolHandler = Callable[[dict, ToolContext], Awaitable[str]]


class RunRecorder(Protocol):
    """Persists the loop's steps (the SQL impl + tables arrive in P4.4b). A
    protocol so the loop is testable without a database, like UsageRecorder;
    recording must never break a turn."""

    async def step(self, *, idx: int, kind: str, name: str, ok: bool, cost_tokens: int) -> None: ...


@dataclass(frozen=True)
class AgentResult:
    text: str
    stop_reason: str  # end_turn | max_steps | too_many_errors | budget
    steps: int
    cost_tokens: int


@dataclass(frozen=True)
class _Dispatched:
    """One tool call's outcome: the result fed back to the model, plus what it
    surfaced for the UI (sources, a staged proposal, entities, a rich view)."""

    result: ToolResult
    sources: tuple[NoteSource, ...]
    proposal: ProposalRef | None
    entities: tuple[EntityRef, ...]
    view: ViewPayload | None


class AgentLoop:
    def __init__(
        self,
        router: LlmRouter,
        registry: ToolRegistry,
        *,
        recorder: RunRecorder | None = None,
        guardrails: Guardrails | None = None,
        task: str = "agent.turn",
    ):
        self._router = router
        self._registry = registry
        self._recorder = recorder
        self._g = guardrails or Guardrails()
        self._task = task

    async def run(
        self,
        *,
        session: SessionContext,
        scopes: Sequence[str],
        conversation: Sequence[LlmMessage],
    ) -> AgentResult:
        scopes = tuple(scopes)
        tools = self._registry.schemas_for(scopes)
        messages: list[LlmMessage] = list(conversation)
        tool_ctx = ToolContext(session=session, scopes=scopes)
        cost = 0
        consecutive_errors = 0
        idx = 0

        for step in range(self._g.max_steps):
            turn = await self._router.converse(
                self._task,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
                strength=SYSTEM_STRENGTH,
            )
            cost += turn.usage.input_tokens + turn.usage.output_tokens
            await self._record(
                idx,
                "model",
                "converse",
                ok=True,
                cost_tokens=turn.usage.input_tokens + turn.usage.output_tokens,
            )
            idx += 1

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                return AgentResult(turn.text, "end_turn", step + 1, cost)
            if cost >= self._g.max_cost_tokens:
                return AgentResult(turn.text, "budget", step + 1, cost)

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                dispatched = await self._dispatch(call, tool_ctx)
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                idx += 1
            messages.append(ToolResultMessage(results=results))

            consecutive_errors = consecutive_errors + 1 if any_error else 0
            if consecutive_errors >= self._g.max_consecutive_tool_errors:
                return AgentResult(turn.text, "too_many_errors", step + 1, cost)

        return AgentResult("", "max_steps", self._g.max_steps, cost)

    async def run_stream(
        self,
        *,
        session: SessionContext,
        scopes: Sequence[str],
        conversation: Sequence[LlmMessage],
    ) -> AsyncIterator[ChatEvent]:
        """The streaming twin of `run`: the same turn loop and guardrails, but it
        yields ChatEvents as they happen — `text_delta` per streamed chunk,
        `tool_call`/`tool_result` at dispatch, and a terminal `done` carrying the
        same stop reason `run` would return. /chat serializes these as SSE.

        Guardrail accounting is identical to `run` so the two paths agree; the
        answer is only ever streamed (the deltas), never re-emitted whole."""
        scopes = tuple(scopes)
        tools = self._registry.schemas_for(scopes)
        messages: list[LlmMessage] = list(conversation)
        tool_ctx = ToolContext(session=session, scopes=scopes)
        cost = 0
        consecutive_errors = 0
        idx = 0

        for _step in range(self._g.max_steps):
            turn: LlmTurn | None = None
            async for part in self._router.converse_stream(
                self._task,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
                strength=SYSTEM_STRENGTH,
            ):
                if isinstance(part, TextChunk):
                    if part.text:
                        yield TextDelta(text=part.text)
                else:
                    turn = part
            if turn is None:
                # The adapter always closes a stream with an LlmTurn; guard the
                # contract anyway rather than dereference None.
                yield DoneEvent(stop_reason="end_turn")
                return
            cost += turn.usage.input_tokens + turn.usage.output_tokens
            await self._record(
                idx,
                "model",
                "converse",
                ok=True,
                cost_tokens=turn.usage.input_tokens + turn.usage.output_tokens,
            )
            idx += 1

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                yield DoneEvent(stop_reason="end_turn")
                return
            if cost >= self._g.max_cost_tokens:
                yield DoneEvent(stop_reason="budget")
                return

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                yield ToolCallEvent(id=call.id, name=call.name, arguments=call.arguments)
                dispatched = await self._dispatch(call, tool_ctx)
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                yield ToolResultEvent(
                    tool_call_id=call.id,
                    ok=not dispatched.result.is_error,
                    summary=dispatched.result.content,
                    sources=list(dispatched.sources),
                    proposal=dispatched.proposal,
                    entities=list(dispatched.entities),
                )
                if dispatched.view is not None:
                    yield ToolViewEvent(tool_call_id=call.id, view=dispatched.view)
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                idx += 1
            messages.append(ToolResultMessage(results=results))

            consecutive_errors = consecutive_errors + 1 if any_error else 0
            if consecutive_errors >= self._g.max_consecutive_tool_errors:
                yield DoneEvent(stop_reason="too_many_errors")
                return

        yield DoneEvent(stop_reason="max_steps")

    async def _dispatch(self, call: ToolCall, tool_ctx: ToolContext) -> _Dispatched:
        if call.name not in self._registry:
            # The model was only offered in-scope tools; an unknown name is a
            # model slip — surface it as a recoverable error, not a crash.
            err = ToolResult(
                tool_call_id=call.id, content=f"unknown tool: {call.name}", is_error=True
            )
            return _Dispatched(err, (), None, (), None)
        tool = self._registry.get(call.name)
        try:
            observation = await tool.handler(call.arguments, tool_ctx)
        except Exception as exc:  # noqa: BLE001 — a tool error is an observation
            log.warning("agent.tool_error", tool=call.name, error=repr(exc))
            err = ToolResult(tool_call_id=call.id, content=f"error: {exc}", is_error=True)
            return _Dispatched(err, (), None, (), None)
        out = observation if isinstance(observation, ToolOutput) else None
        result = ToolResult(tool_call_id=call.id, content=str(observation), is_error=False)
        return _Dispatched(
            result,
            out.sources if out else (),
            out.proposal if out else None,
            out.entities if out else (),
            out.view if out else None,
        )

    async def _record(self, idx: int, kind: str, name: str, *, ok: bool, cost_tokens: int) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.step(idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens)
        except Exception as exc:  # noqa: BLE001 — recording must never break a turn
            log.warning("agent.record_failed", error=repr(exc))
