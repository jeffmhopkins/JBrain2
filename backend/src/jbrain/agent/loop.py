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
    JobEnqueuedEvent,
    NoteSource,
    ProposalRef,
    TextDelta,
    ToolCallEvent,
    ToolResultEvent,
    ToolViewEvent,
    VerdictEvent,
    ViewPayload,
)
from jbrain.agent.reflexion import (
    MAX_RETRIES,
    VerificationResult,
    aggregate,
    claims_from,
    critique_worthy,
    reflect,
    verify_grounding,
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
    """What a tool handler receives: the RLS scope its reads must run under, and
    the owner's IANA display timezone (None = UTC) so a tool can render times in
    the owner's zone — its prose then agrees with the client-localized cards."""

    session: SessionContext
    scopes: tuple[str, ...]
    timezone: str | None = None


@dataclass(frozen=True)
class JobRef:
    """A job a long/deferred tool enqueued instead of blocking the turn — the id to
    poll and a one-line summary. The loop surfaces it as a `JobEnqueuedEvent`."""

    job_id: str
    summary: str


class ToolOutput(str):
    """A tool observation that also carries what the tool surfaced for the UI —
    note sources (source cards), a staged proposal (a "Review proposal" chip),
    resolved entities, a rich `view` (a registered component the PWA renders, e.g.
    a checklist), and/or a `job` it deferred to the queue. It *is* the model-facing
    text (a str subclass), so handlers keep their `-> str` contract and existing
    call sites are untouched; `_dispatch` pulls the extras off when present."""

    sources: tuple[NoteSource, ...]
    proposal: ProposalRef | None
    entities: tuple[EntityRef, ...]
    view: ViewPayload | None
    job: JobRef | None

    def __new__(
        cls,
        content: str,
        sources: tuple[NoteSource, ...] = (),
        proposal: ProposalRef | None = None,
        entities: tuple[EntityRef, ...] = (),
        view: ViewPayload | None = None,
        job: JobRef | None = None,
    ) -> "ToolOutput":
        out = super().__new__(cls, content)
        out.sources = sources
        out.proposal = proposal
        out.entities = entities
        out.view = view
        out.job = job
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
    surfaced for the UI (sources, a staged proposal, entities, a rich view, an
    enqueued job)."""

    result: ToolResult
    sources: tuple[NoteSource, ...]
    proposal: ProposalRef | None
    entities: tuple[EntityRef, ...]
    view: ViewPayload | None
    job: JobRef | None


@dataclass(frozen=True)
class _BufferedTurn:
    """One non-streaming produce-step for the opt-in buffer-then-retry mode (a):
    the whole turn run to completion with its ChatEvents *buffered* (not yet
    streamed) plus the reflexion evidence. `reflect` re-runs the producer and keeps
    only the strictly-improving attempt; the kept attempt's buffered events are
    then replayed as the live stream, so the user never sees a discarded draft."""

    events: tuple[ChatEvent, ...]
    answer: str
    sources: tuple[NoteSource, ...]
    mutated: bool
    stop_reason: str
    cost_tokens: int


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
        timezone: str | None = None,
    ) -> AgentResult:
        scopes = tuple(scopes)
        tools = self._registry.schemas_for(scopes)
        messages: list[LlmMessage] = list(conversation)
        tool_ctx = ToolContext(session=session, scopes=scopes, timezone=timezone)
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
        timezone: str | None = None,
        buffer_retry: bool = False,
    ) -> AsyncIterator[ChatEvent]:
        """The streaming twin of `run`: the same turn loop and guardrails, but it
        yields ChatEvents as they happen — `text_delta` per streamed chunk,
        `tool_call`/`tool_result` at dispatch, and a terminal `done` carrying the
        same stop reason `run` would return. /chat serializes these as SSE.

        Guardrail accounting is identical to `run` so the two paths agree; the
        answer is only ever streamed (the deltas), never re-emitted whole.

        Reflexion (Loop 1, docs/ASSISTANT.md) rides at the tail: the loop tracks
        the answer text it streamed, the sources tools surfaced, and whether a
        mutation was staged, then — only when the turn is critique-worthy — runs
        the pure verifiers after the terminal `DoneEvent` and emits a `VerdictEvent`
        if anything failed. The verifiers make **no model call** (they are pure
        token-overlap / scope checks), so verify-and-annotate adds nothing to the
        per-turn cost and the budget; a non-critique turn skips it entirely and its
        stream is byte-for-byte what it was before.

        `buffer_retry` (the opt-in mode (a), off by default) switches a
        critique-worthy turn to buffer-then-retry: the turn is produced
        non-streaming, the verifiers run, and `reflect` may re-produce (strict
        improvement, capped at N=2) before the kept attempt's events stream. This
        trades the live token stream for a spinner while verification clears."""
        if buffer_retry:
            async for ev in self._run_stream_buffered(session, scopes, conversation, timezone):
                yield ev
            return
        scopes = tuple(scopes)
        tools = self._registry.schemas_for(scopes)
        messages: list[LlmMessage] = list(conversation)
        tool_ctx = ToolContext(session=session, scopes=scopes, timezone=timezone)
        cost = 0
        consecutive_errors = 0
        idx = 0
        # Reflexion's evidence for the tail verdict: the streamed answer, the source
        # snippets tools surfaced, and whether any tool staged/declared a mutation.
        answer_parts: list[str] = []
        surfaced_sources: list[NoteSource] = []
        mutated = False

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
                        answer_parts.append(part.text)
                        yield TextDelta(text=part.text)
                else:
                    turn = part
            if turn is None:
                # The adapter always closes a stream with an LlmTurn; guard the
                # contract anyway rather than dereference None.
                async for ev in self._finish(
                    "end_turn", answer_parts, surfaced_sources, mutated, scopes
                ):
                    yield ev
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
                async for ev in self._finish(
                    "end_turn", answer_parts, surfaced_sources, mutated, scopes
                ):
                    yield ev
                return
            if cost >= self._g.max_cost_tokens:
                async for ev in self._finish(
                    "budget", answer_parts, surfaced_sources, mutated, scopes
                ):
                    yield ev
                return

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                yield ToolCallEvent(id=call.id, name=call.name, arguments=call.arguments)
                dispatched = await self._dispatch(call, tool_ctx)
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                surfaced_sources.extend(dispatched.sources)
                # A staged Proposal, or a tool whose spec declares it mutating, makes
                # the turn critique-worthy — it carried a write, not just a read.
                mutated = mutated or dispatched.proposal is not None or self._is_mutating(call.name)
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
                if dispatched.job is not None:
                    # A long/deferred tool handed the work to the queue rather than
                    # blocking the turn; tell the client what is now running.
                    yield JobEnqueuedEvent(
                        job_id=dispatched.job.job_id, summary=dispatched.job.summary
                    )
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                idx += 1
            messages.append(ToolResultMessage(results=results))

            consecutive_errors = consecutive_errors + 1 if any_error else 0
            if consecutive_errors >= self._g.max_consecutive_tool_errors:
                async for ev in self._finish(
                    "too_many_errors", answer_parts, surfaced_sources, mutated, scopes
                ):
                    yield ev
                return

        async for ev in self._finish("max_steps", answer_parts, surfaced_sources, mutated, scopes):
            yield ev

    async def _run_stream_buffered(
        self,
        session: SessionContext,
        scopes: Sequence[str],
        conversation: Sequence[LlmMessage],
        timezone: str | None,
    ) -> AsyncIterator[ChatEvent]:
        """Mode (a): produce the turn non-streaming, run `reflect` (strict
        improvement, N=2 cap), then replay the kept attempt's buffered events as the
        live stream + the tail verdict. Retries are bounded by `reflect`'s hard cap
        AND by the loop's `max_cost_tokens` guardrail — a shared budget across
        attempts — so reflexion can never overspend the per-turn cap. This spend is
        the ordinary per-turn budget, NOT the self-improvement budget (a live
        interactive turn must not be starved by a nightly eval)."""
        scopes = tuple(scopes)
        budget = [self._g.max_cost_tokens]  # mutable: shared remaining cap across attempts
        incumbent: list[tuple[_BufferedTurn, VerificationResult] | None] = [None]

        async def produce() -> tuple[_BufferedTurn, VerificationResult]:
            # Once the per-turn cost cap is spent, stop re-producing: hand back the
            # incumbent with its own (non-improving) score so `reflect`'s strict-
            # improvement rule keeps the best answer so far and makes no further
            # model call. This bounds reflexion by Guardrails.max_cost_tokens — the
            # ordinary per-turn budget, NOT the self-improvement budget.
            if budget[0] <= 0 and incumbent[0] is not None:
                return incumbent[0]
            turn = await self._produce_buffered(session, scopes, conversation, timezone, budget)
            verdict = aggregate(
                [verify_grounding(claims_from(turn.answer), [s.snippet for s in turn.sources])]
            )
            if incumbent[0] is None:
                incumbent[0] = (turn, verdict)
            return turn, verdict

        first, verdict = await produce()
        if critique_worthy(source_count=len(first.sources), mutated=first.mutated, scopes=scopes):
            reflection = await reflect(
                lambda: produce(),
                max_retries=MAX_RETRIES,
                seed=(first, verdict),
            )
            kept, kept_verdict = reflection.answer, reflection.result
        else:
            kept, kept_verdict = first, verdict

        for ev in kept.events:
            yield ev
        yield DoneEvent(stop_reason=kept.stop_reason)
        if not kept_verdict.passed and critique_worthy(
            source_count=len(kept.sources), mutated=kept.mutated, scopes=scopes
        ):
            yield VerdictEvent(
                passed=False, score=kept_verdict.score, issues=list(kept_verdict.issues)
            )

    async def _produce_buffered(
        self,
        session: SessionContext,
        scopes: tuple[str, ...],
        conversation: Sequence[LlmMessage],
        timezone: str | None,
        budget: list[int],
    ) -> _BufferedTurn:
        """One full non-streaming produce-step for mode (a): run the turn loop to a
        terminal stop, buffering the ChatEvents it would have streamed (so a
        discarded retry never reaches the user). Shares the remaining cost cap in
        `budget` so retries cannot overspend the per-turn guardrail."""
        tools = self._registry.schemas_for(scopes)
        messages: list[LlmMessage] = list(conversation)
        tool_ctx = ToolContext(session=session, scopes=scopes, timezone=timezone)
        events: list[ChatEvent] = []
        answer_parts: list[str] = []
        sources: list[NoteSource] = []
        mutated = False
        idx = 0
        spent = 0

        for _step in range(self._g.max_steps):
            turn = await self._router.converse(
                self._task,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
                strength=SYSTEM_STRENGTH,
            )
            spent = turn.usage.input_tokens + turn.usage.output_tokens
            budget[0] -= spent
            await self._record(idx, "model", "converse", ok=True, cost_tokens=spent)
            idx += 1
            if turn.text:
                answer_parts.append(turn.text)
                events.append(TextDelta(text=turn.text))

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                return _BufferedTurn(
                    tuple(events), "".join(answer_parts), tuple(sources), mutated, "end_turn", spent
                )
            if budget[0] <= 0:
                return _BufferedTurn(
                    tuple(events), "".join(answer_parts), tuple(sources), mutated, "budget", spent
                )

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                events.append(ToolCallEvent(id=call.id, name=call.name, arguments=call.arguments))
                dispatched = await self._dispatch(call, tool_ctx)
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                sources.extend(dispatched.sources)
                mutated = mutated or dispatched.proposal is not None or self._is_mutating(call.name)
                events.append(
                    ToolResultEvent(
                        tool_call_id=call.id,
                        ok=not dispatched.result.is_error,
                        summary=dispatched.result.content,
                        sources=list(dispatched.sources),
                        proposal=dispatched.proposal,
                        entities=list(dispatched.entities),
                    )
                )
                if dispatched.view is not None:
                    events.append(ToolViewEvent(tool_call_id=call.id, view=dispatched.view))
                if dispatched.job is not None:
                    events.append(
                        JobEnqueuedEvent(
                            job_id=dispatched.job.job_id, summary=dispatched.job.summary
                        )
                    )
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                idx += 1
            messages.append(ToolResultMessage(results=results))
            if any_error:
                return _BufferedTurn(
                    tuple(events),
                    "".join(answer_parts),
                    tuple(sources),
                    mutated,
                    "too_many_errors",
                    spent,
                )

        return _BufferedTurn(
            tuple(events), "".join(answer_parts), tuple(sources), mutated, "max_steps", spent
        )

    def _is_mutating(self, name: str) -> bool:
        """Whether a dispatched tool declares a write/sensitive effect — the
        mutation signal Reflexion's trigger reads. An unknown name (a model slip)
        is never mutating."""
        if name not in self._registry:
            return False
        spec = self._registry.get(name).spec
        return spec.mutating or spec.side_effecting or spec.permission in ("mutate", "sensitive")

    async def _finish(
        self,
        stop_reason: str,
        answer_parts: list[str],
        sources: list[NoteSource],
        mutated: bool,
        scopes: tuple[str, ...],
    ) -> AsyncIterator[ChatEvent]:
        """Close the stream: emit the terminal `DoneEvent`, then — in the default
        verify-and-annotate mode, only when the turn is critique-worthy and the
        pure verifiers flag something — a tail `VerdictEvent`. No model call, no
        retry, no persistence: the answer the user saw stands, annotated."""
        yield DoneEvent(stop_reason=stop_reason)
        if not critique_worthy(source_count=len(sources), mutated=mutated, scopes=scopes):
            return
        verdict = aggregate(
            [verify_grounding(claims_from("".join(answer_parts)), [s.snippet for s in sources])]
        )
        if not verdict.passed:
            yield VerdictEvent(passed=False, score=verdict.score, issues=list(verdict.issues))

    async def _dispatch(self, call: ToolCall, tool_ctx: ToolContext) -> _Dispatched:
        if call.name not in self._registry:
            # The model was only offered in-scope tools; an unknown name is a
            # model slip — surface it as a recoverable error, not a crash.
            err = ToolResult(
                tool_call_id=call.id, content=f"unknown tool: {call.name}", is_error=True
            )
            return _Dispatched(err, (), None, (), None, None)
        tool = self._registry.get(call.name)
        try:
            observation = await tool.handler(call.arguments, tool_ctx)
        except Exception as exc:  # noqa: BLE001 — a tool error is an observation
            log.warning("agent.tool_error", tool=call.name, error=repr(exc))
            err = ToolResult(tool_call_id=call.id, content=f"error: {exc}", is_error=True)
            return _Dispatched(err, (), None, (), None, None)
        out = observation if isinstance(observation, ToolOutput) else None
        result = ToolResult(tool_call_id=call.id, content=str(observation), is_error=False)
        return _Dispatched(
            result,
            out.sources if out else (),
            out.proposal if out else None,
            out.entities if out else (),
            out.view if out else None,
            out.job if out else None,
        )

    async def _record(self, idx: int, kind: str, name: str, *, ok: bool, cost_tokens: int) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.step(idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens)
        except Exception as exc:  # noqa: BLE001 — recording must never break a turn
            log.warning("agent.record_failed", error=repr(exc))
