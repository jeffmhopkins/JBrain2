"""The agent turn loop: a thin ReAct cycle over the LLM adapter.

Assemble the conversation, ask the model with the in-scope tools, run any tool
calls it makes, feed the results back, and repeat until it answers or a guardrail
trips. The loop owns the guardrails — step, cost, and consecutive-error caps —
and never trusts the model to stop itself. Tool dispatch and the run record are
the loop's concern; what a tool *does* is the handler's.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

import structlog

from jbrain.agent.contracts import (
    ChatEvent,
    DoneEvent,
    EntityRef,
    GeneralKnowledgeEvent,
    JobEnqueuedEvent,
    NoteSource,
    ProposalRef,
    ReasoningDelta,
    TextDelta,
    ToolCallEvent,
    ToolProgressEvent,
    ToolResultEvent,
    ToolViewEvent,
    UsageEvent,
    VerdictEvent,
    ViewPayload,
    WebSource,
)
from jbrain.agent.reflexion import (
    MAX_RETRIES,
    PASS_SCORE,
    SENSITIVE_SCOPES,
    VerificationResult,
    aggregate,
    claims_from,
    critique_worthy,
    has_substantive_claim,
    reflect,
    ungrounded_claims,
    verify_grounding,
)
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.tree import TreeState
from jbrain.db.session import SessionContext
from jbrain.llm import (
    AssistantMessage,
    LlmMessage,
    LlmRouter,
    LlmTurn,
    LlmUsage,
    ReasoningChunk,
    TextChunk,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    UserMessage,
)
from jbrain.llm.promptfile import load_prompt

log = structlog.get_logger()

_SYSTEM = load_prompt(Path(__file__).parent / "prompts" / "system.prompt")
SYSTEM_PROMPT: str = _SYSTEM.render()
SYSTEM_VERSION: str = _SYSTEM.version
SYSTEM_STRENGTH: str = _SYSTEM.strength

# Per-turn generation budget. A local reasoning model (gpt-oss/GLM) bills its
# thinking trace against this cap before any answer or tool call, so the budget
# must leave generous headroom for the trace on top of the visible turn — the
# default 4096 risked truncating a long answer mid-stream. Applies per ReAct step,
# not per chain.
TURN_MAX_TOKENS: int = 16384


def _grounding_corpus(sources: Sequence[NoteSource], entities: Sequence[EntityRef]) -> list[str]:
    """The texts a claim may ground against: note snippets PLUS each retrieved
    entity's canonical label, every alias, and its current-fact statements. A turn
    answered from the entity graph (find_entity/read_entity → EntityRefs, zero
    NoteSources) would otherwise verify against an empty corpus and every claim would
    score 0 — so "What is my name?" answered "Jeffrey Mark Hopkins (Jeff)" grounds
    against those aliases, and "what year was I born?" answered "1986" grounds against
    the read entity's birthDate fact, instead of being falsely flagged "not in your
    notes"."""
    corpus = [s.snippet for s in sources]
    for entity in entities:
        corpus.append(entity.label)
        corpus.extend(entity.aliases)
        corpus.extend(entity.facts)
    return corpus


def _touched_sensitive(sources: Sequence[NoteSource], entities: Sequence[EntityRef]) -> bool:
    """Whether the turn actually surfaced sensitive-domain data — a source or entity
    whose domain is health|finance|location. The Reflexion sensitive-scope trigger
    reads THIS, not the session's held scopes: Full Brain always holds every scope,
    so a scope-membership test would flag every Full Brain turn. A turn only carries
    real-world consequence when it touched the consequential data itself."""
    return any(s.domain in SENSITIVE_SCOPES for s in sources) or any(
        e.domain in SENSITIVE_SCOPES for e in entities
    )


@dataclass(frozen=True)
class Guardrails:
    """Hard limits the loop enforces, never the model. A run that hits one stops
    with the corresponding stop reason rather than spinning or overspending."""

    max_steps: int = 20
    max_cost_tokens: int = 200_000
    max_consecutive_tool_errors: int = 3


# A model set to think harder earns a deeper tool budget: a longer ReAct chain (more
# searches/reads) before the step cap stops it. low/none/non-reasoning keep the default.
# (Doubled from 10/15/20 alongside the per-child caps so a heavy jerv turn rarely
# truncates mid-chain; the cost-token backstop still bounds a runaway.)
STEPS_BY_EFFORT: dict[str, int] = {"high": 40, "medium": 30}

# The forced-final synthesis (force_final_answer, on step exhaustion) writes an answer
# from already-gathered material — a mechanical step that needs no thinking. Run it at
# NONE effort regardless of the run's effort: even "low" still let gpt-oss generate a
# huge hidden reasoning trace (~74s at ~3 tok/s on the local box) that looked like a
# stall — "none" skips the trace so the synthesis is fast.
FINAL_ANSWER_EFFORT = "none"

# The forced-final turn carries NO tools, but gpt-oss (trained to reach for a tool) will
# still emit its NEXT intended search as plain text when merely asked to continue — so a
# step-capped child's "answer" comes back as a raw tool-call JSON ({"query": ...}) instead
# of prose. An explicit directive turns it back into a synthesis: use what's gathered, no
# tool calls, no JSON. Appended as a final user turn so it's the model's last instruction.
FINAL_ANSWER_DIRECTIVE = (
    "You are out of research budget and can call no more tools. Using ONLY what you have "
    "already gathered above, write your final answer now as prose. Do not emit a tool "
    "call, a search query, or JSON — just the synthesized answer."
)

# Soft landing: a few steps before the HARD step cap, a force-final-eligible run (a
# sub-agent) is nudged to stop searching and synthesize, so it ends cleanly on its own
# (end_turn) instead of being force-cut at max_steps (which reads as "truncated"). The
# forced-final turn above is the fallback if it ignores the nudge. An ordinary turn (no
# force_final_answer) is never budget-warned.
_BUDGET_WARNING_LEAD = 3  # ReAct steps before the cap to issue the warning
BUDGET_WARNING_DIRECTIVE = (
    "You are almost out of tool-call budget. Make at most one more essential tool call "
    "if truly necessary, then STOP using tools and write your complete final answer now "
    "from what you have already gathered."
)


def guardrails_for_effort(effort: str | None, *, scale: int = 1) -> Guardrails:
    """The loop's budget sized to the task's effective reasoning effort, then scaled
    by a per-agent factor. `scale` (an agent's `budget_multiplier`, default 1) widens
    BOTH the step cap and the cost-token budget together: the archivist's long, many-
    tool mailbox cleanups run at 4, so a single sweep isn't cut off mid-chain
    (docs/EMAIL_ARCHIVIST_PLAN.md). The consecutive-error cap is unscaled — a wedged
    chain should still bail fast regardless of persona."""
    base = STEPS_BY_EFFORT.get(effort or "", Guardrails.max_steps)
    return Guardrails(
        max_steps=base * scale,
        max_cost_tokens=Guardrails.max_cost_tokens * scale,
    )


@dataclass(frozen=True)
class ToolContext:
    """What a tool handler receives: the RLS scope its reads must run under, and
    the owner's IANA display timezone (None = UTC) so a tool can render times in
    the owner's zone — its prose then agrees with the client-localized cards.

    `agent_session_id` is the chat session this turn belongs to, so a tool that
    stages a Proposal can tie it to the session (the review inbox scopes by it).
    None for non-chat callers (e.g. the wiki Editor) and background loops, which
    stage session-less proposals that surface in every session's inbox.

    `here` is the owner's (latitude, longitude) for this turn — the warm geolocation
    fix the PWA attached, or, when this turn carried none, the owner's cached
    last-known warm fix. It lets a location tool answer from the phone's current (or
    most recent) position rather than only the OwnTracks device stack. `here_as_of`
    is None when `here` is this turn's live fix and the fix's capture time when it is
    the cached fallback — so the tool labels a stale position honestly and never
    reports it as "here now"."""

    session: SessionContext
    scopes: tuple[str, ...]
    timezone: str | None = None
    agent_session_id: str | None = None
    here: tuple[float, float] | None = None
    here_as_of: datetime | None = None
    # Sub-agent spawning context (docs/SUBAGENT_SPAWNING_PLAN.md). `depth` is this
    # turn's depth in the agent tree (root=0); spawn is refused unless depth == 0 —
    # only the root jerv fans out, and its children are leaves (nesting removed).
    # `agent_tools` is THIS turn's effective allowed tool names — the ceiling the
    # spawn handler clamps a child to (child effective tools ⊆ parent's, enforced at
    # dispatch). `tree` is the per-root-turn shared fan state (the total-agents cap,
    # and in Wave S2 the token budget); `run_id` is this turn's run for stamping a
    # child run's parent_run_id. All default to the root/no-spawn case so every
    # existing call site is unchanged.
    depth: int = 0
    agent_tools: frozenset[str] = frozenset()
    tree: TreeState | None = None
    run_id: str | None = None
    # Mid-execution progress sink, set only on the streaming path: a tool calls it with
    # (step, total, preview_data_uri | None, label | None) and the loop turns each call
    # into an ephemeral ToolProgressEvent on the turn's SSE. Image gen sends a step bar +
    # preview; a multi-phase tool (analyze_video) sends a `label` per phase. Sync +
    # fire-and-forget; None for the batch path and tools that don't report progress.
    emit_progress: Callable[[int, int, str | None, str | None], None] | None = None
    # Generalized live-event sink (Wave S2), set only on the streaming path: a tool
    # whose work is itself a stream of events (the spawn handler's `subagent_*` fan)
    # pushes whole ChatEvents the loop forwards onto the turn's SSE — drained
    # concurrently with the awaited tool, exactly like `emit_progress`. The loop
    # injects the dispatching call's id so the events anchor under it. Sync +
    # fire-and-forget; None for the batch path and tools that emit no events.
    emit_event: Callable[[ChatEvent], None] | None = None


@dataclass(frozen=True)
class JobRef:
    """A job a long/deferred tool enqueued instead of blocking the turn — the id to
    poll and a one-line summary. The loop surfaces it as a `JobEnqueuedEvent`."""

    job_id: str
    summary: str


class ToolOutput(str):
    """A tool observation that also carries what the tool surfaced for the UI —
    note sources (source cards), web sources (favicon citation chips), a staged
    proposal (a "Review proposal" chip), resolved entities, a rich `view` (a
    registered component the PWA renders, e.g. a checklist), and/or a `job` it
    deferred to the queue. It *is* the model-facing text (a str subclass), so
    handlers keep their `-> str` contract and existing call sites are untouched;
    `_dispatch` pulls the extras off when present."""

    sources: tuple[NoteSource, ...]
    web_sources: tuple[WebSource, ...]
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
        web_sources: tuple[WebSource, ...] = (),
    ) -> "ToolOutput":
        out = super().__new__(cls, content)
        out.sources = sources
        out.web_sources = web_sources
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
    web_sources: tuple[WebSource, ...] = ()


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
    entities: tuple[EntityRef, ...]
    mutated: bool
    stop_reason: str


def _buffered_critique_worthy(turn: "_BufferedTurn") -> bool:
    """The Loop-1 trigger applied to a buffered turn: evidence (sources OR entities),
    a mutation, or sensitive data actually touched (not merely a held scope)."""
    return critique_worthy(
        source_count=len(turn.sources),
        entity_count=len(turn.entities),
        mutated=turn.mutated,
        touched_sensitive=_touched_sensitive(turn.sources, turn.entities),
    )


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

    @staticmethod
    def _tree_exhausted(tree: TreeState | None, depth: int) -> bool:
        """Whether this loop must stop on the shared tree budget (Wave S2). The root
        (depth 0) may spend the whole pool; a child (depth >= 1) stops at the
        children's pool so the root's reserve survives for synthesis. A turn with no
        tree, or a tree with no seeded budget, is governed only by its own per-loop
        Guardrails (returns False)."""
        if tree is None:
            return False
        return tree.root_exhausted() if depth == 0 else tree.children_exhausted()

    async def _converse_turn(
        self,
        system_prompt: str,
        messages: Sequence[LlmMessage],
        tools: Sequence[object],
        reasoning_effort: str | None,
        on_text: Callable[[str], None] | None,
        on_reasoning: Callable[[str], None] | None,
    ) -> LlmTurn:
        """One model turn for `run`. With no streaming callbacks it's a plain
        `converse` (the existing non-streaming path, unchanged). With `on_text`/
        `on_reasoning` it streams via `converse_stream` and forwards each chunk to the
        callback as it arrives — the sub-agent spawner uses this to surface a child's
        live tokens — while still returning the closing turn so the loop is identical."""
        if on_text is None and on_reasoning is None:
            return await self._router.converse(
                self._task,
                system=system_prompt,
                messages=messages,
                tools=tools,  # type: ignore[arg-type]
                max_tokens=TURN_MAX_TOKENS,
                strength=SYSTEM_STRENGTH,
                effort_override=reasoning_effort,
            )
        turn: LlmTurn | None = None
        async for part in self._router.converse_stream(
            self._task,
            system=system_prompt,
            messages=messages,
            tools=tools,  # type: ignore[arg-type]
            max_tokens=TURN_MAX_TOKENS,
            strength=SYSTEM_STRENGTH,
            effort_override=reasoning_effort,
        ):
            if isinstance(part, TextChunk):
                if part.text and on_text is not None:
                    on_text(part.text)
            elif isinstance(part, ReasoningChunk):
                if part.text and on_reasoning is not None:
                    on_reasoning(part.text)
            else:
                turn = part
        # The adapter always closes a stream with an LlmTurn; guard the contract.
        return turn or LlmTurn(text="", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(0, 0))

    async def run(
        self,
        *,
        session: SessionContext,
        scopes: Sequence[str],
        conversation: Sequence[LlmMessage],
        timezone: str | None = None,
        system: str | None = None,
        agent_session_id: str | None = None,
        tools_allow: frozenset[str] | None = None,
        depth: int = 0,
        tree: TreeState | None = None,
        run_id: str | None = None,
        on_step: Callable[[int, int], None] | None = None,
        reasoning_effort: str | None = None,
        force_final_answer: bool = False,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict, bool], None] | None = None,
        # Per-model-call usage (input_tokens, output_tokens) — the fullest the context
        # has been this call. The spawn service forwards it as a child context-fill meter,
        # the non-streaming twin of run_stream's UsageEvent.
        on_usage: Callable[[int, int], None] | None = None,
    ) -> AgentResult:
        scopes = tuple(scopes)
        tools = self._registry.schemas_for(scopes, tools_allow)
        allowed = self._registry.allowed_names(scopes, tools_allow)
        messages: list[LlmMessage] = list(conversation)
        # `agent_tools=allowed` is this turn's effective ceiling — a child this turn
        # spawns is clamped to it (docs/SUBAGENT_SPAWNING_PLAN.md, the parent⊆child clamp).
        tool_ctx = ToolContext(
            session=session,
            scopes=scopes,
            timezone=timezone,
            agent_session_id=agent_session_id,
            depth=depth,
            agent_tools=allowed,
            tree=tree,
            run_id=run_id,
        )
        # A caller can swap the system prompt (the wiki Editor uses its own persona); existing
        # callers pass nothing and keep the Full Brain prompt — fully backward-compatible.
        system_prompt = system or SYSTEM_PROMPT
        cost = 0
        consecutive_errors = 0
        idx = 0

        for step in range(self._g.max_steps):
            # Soft landing (sub-agents only): a few steps before the hard cap, ask the
            # model to wrap up so it lands on end_turn rather than being force-cut at the
            # cap. Fired once; the forced-final answer below still catches a model that
            # ignores it.
            if force_final_answer and step > 0 and step == self._g.max_steps - _BUDGET_WARNING_LEAD:
                messages.append(UserMessage(text=BUDGET_WARNING_DIRECTIVE))
            turn = await self._converse_turn(
                system_prompt, messages, tools, reasoning_effort, on_text, on_reasoning
            )
            spent_call = turn.usage.input_tokens + turn.usage.output_tokens
            cost += spent_call
            if tree is not None:
                tree.charge(spent_call)
            if on_usage is not None:
                on_usage(turn.usage.input_tokens, turn.usage.output_tokens)
            await self._record(
                idx,
                "model",
                "converse",
                ok=True,
                cost_tokens=spent_call,
            )
            idx += 1
            # Per-step progress hook (Wave S2 follow-up): the spawn service uses it to
            # stream a live subagent_progress per child step so the UI's budget meter
            # and step count move while a child works (children run non-streaming).
            if on_step is not None:
                on_step(step + 1, cost)

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                return AgentResult(turn.text, "end_turn", step + 1, cost)
            if self._tree_exhausted(tree, depth):
                return AgentResult(turn.text, "tree_budget_exhausted", step + 1, cost)
            if cost >= self._g.max_cost_tokens:
                return AgentResult(turn.text, "budget", step + 1, cost)

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                dispatched = await self._dispatch(call, tool_ctx, allowed)
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                # Surface the tool step to a caller streaming the run (the sub-agent fan's
                # live "Worked" list); the args go too so it can show what was searched.
                if on_tool is not None:
                    on_tool(call.name, call.arguments, not dispatched.result.is_error)
                idx += 1
            messages.append(ToolResultMessage(results=results))

            consecutive_errors = consecutive_errors + 1 if any_error else 0
            if consecutive_errors >= self._g.max_consecutive_tool_errors:
                return AgentResult(turn.text, "too_many_errors", step + 1, cost)

        if force_final_answer:
            # Out of steps mid-chain. Rather than return an empty "(no answer)", make one
            # final turn with NO tools so the model must synthesize an answer from what it
            # already gathered — a research child otherwise reports nothing the moment it
            # hits the cap. Still flagged `max_steps` so the caller knows it's step-limited.
            # The directive (a final user turn) keeps gpt-oss from emitting its next search
            # as text instead of synthesizing — see FINAL_ANSWER_DIRECTIVE.
            final_messages = [*messages, UserMessage(text=FINAL_ANSWER_DIRECTIVE)]
            final = await self._converse_turn(
                system_prompt, final_messages, (), FINAL_ANSWER_EFFORT, on_text, on_reasoning
            )
            spent_final = final.usage.input_tokens + final.usage.output_tokens
            cost += spent_final
            if tree is not None:
                tree.charge(spent_final)
            if on_usage is not None:
                on_usage(final.usage.input_tokens, final.usage.output_tokens)
            await self._record(idx, "model", "converse", ok=True, cost_tokens=spent_final)
            return AgentResult(final.text, "max_steps", self._g.max_steps, cost)
        return AgentResult("", "max_steps", self._g.max_steps, cost)

    async def run_stream(
        self,
        *,
        session: SessionContext,
        scopes: Sequence[str],
        conversation: Sequence[LlmMessage],
        timezone: str | None = None,
        buffer_retry: bool = False,
        agent_session_id: str | None = None,
        system: str | None = None,
        tools_allow: frozenset[str] | None = None,
        general_knowledge_label: bool = True,
        here: tuple[float, float] | None = None,
        here_as_of: datetime | None = None,
        context_window: int | None = None,
        depth: int = 0,
        tree: TreeState | None = None,
        run_id: str | None = None,
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
        trades the live token stream for a spinner while verification clears.

        `context_window`, when given, drives a `UsageEvent` emitted after each model
        turn so the PWA can show a live context-usage meter (None suppresses it, so a
        caller/test that doesn't care gets the byte-for-byte stream it always had)."""
        if buffer_retry:
            async for ev in self._run_stream_buffered(
                session,
                scopes,
                conversation,
                timezone,
                agent_session_id,
                system,
                tools_allow,
                general_knowledge_label,
                here,
                here_as_of,
                context_window,
                depth,
                tree,
                run_id,
            ):
                yield ev
            return
        scopes = tuple(scopes)
        # The selected agent supplies its persona prompt and tool allowlist
        # (docs/ASSISTANT.md "Agent selection"); the default is the Full Brain curator.
        system_prompt = system or SYSTEM_PROMPT
        tools = self._registry.schemas_for(scopes, tools_allow)
        allowed = self._registry.allowed_names(scopes, tools_allow)
        messages: list[LlmMessage] = list(conversation)
        # A tool may emit live items mid-execution onto one queue the per-call dispatch
        # below drains: a (step, total, preview, label) tuple becomes a ToolProgressEvent
        # (image gen / multi-phase tools), and a whole ChatEvent (the spawn handler's
        # `subagent_*` fan) is forwarded as-is. Tool calls run one at a time, so every
        # enqueued item belongs to the call currently dispatching.
        live_q: asyncio.Queue[tuple[int, int, str | None, str | None] | ChatEvent | None] = (
            asyncio.Queue()
        )
        tool_ctx = ToolContext(
            session=session,
            scopes=scopes,
            timezone=timezone,
            agent_session_id=agent_session_id,
            here=here,
            here_as_of=here_as_of,
            depth=depth,
            agent_tools=allowed,
            tree=tree,
            run_id=run_id,
            emit_progress=lambda step, total, preview, label: live_q.put_nowait(
                (step, total, preview, label)
            ),
            emit_event=live_q.put_nowait,
        )
        cost = 0
        consecutive_errors = 0
        idx = 0
        # Reflexion's evidence for the tail verdict: the streamed answer, the source
        # snippets tools surfaced, and whether any tool staged/declared a mutation.
        answer_parts: list[str] = []
        surfaced_sources: list[NoteSource] = []
        surfaced_entities: list[EntityRef] = []
        mutated = False

        for _step in range(self._g.max_steps):
            turn: LlmTurn | None = None
            async for part in self._router.converse_stream(
                self._task,
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=TURN_MAX_TOKENS,
                strength=SYSTEM_STRENGTH,
            ):
                if isinstance(part, TextChunk):
                    if part.text:
                        answer_parts.append(part.text)
                        yield TextDelta(text=part.text)
                elif isinstance(part, ReasoningChunk):
                    # The model's thinking trace — streamed to the PWA's "thinking"
                    # disclosure, never added to the answer or the grounding corpus.
                    if part.text:
                        yield ReasoningDelta(text=part.text)
                else:
                    turn = part
            if turn is None:
                # The adapter always closes a stream with an LlmTurn; guard the
                # contract anyway rather than dereference None.
                async for ev in self._finish(
                    "end_turn",
                    answer_parts,
                    surfaced_sources,
                    surfaced_entities,
                    mutated,
                    general_knowledge_label,
                ):
                    yield ev
                return
            spent_call = turn.usage.input_tokens + turn.usage.output_tokens
            cost += spent_call
            if tree is not None:
                tree.charge(spent_call)
            await self._record(
                idx,
                "model",
                "converse",
                ok=True,
                cost_tokens=spent_call,
            )
            idx += 1
            # Live context accounting: this step's prompt is the fullest the context
            # has been, so the PWA's meter tracks the latest UsageEvent. Suppressed
            # when the caller gave no window (tests, non-/chat callers).
            if context_window is not None:
                yield UsageEvent(
                    input_tokens=turn.usage.input_tokens,
                    output_tokens=turn.usage.output_tokens,
                    context_window=context_window,
                )

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                async for ev in self._finish(
                    "end_turn",
                    answer_parts,
                    surfaced_sources,
                    surfaced_entities,
                    mutated,
                    general_knowledge_label,
                ):
                    yield ev
                return
            if self._tree_exhausted(tree, depth):
                async for ev in self._finish(
                    "tree_budget_exhausted",
                    answer_parts,
                    surfaced_sources,
                    surfaced_entities,
                    mutated,
                    general_knowledge_label,
                ):
                    yield ev
                return
            if cost >= self._g.max_cost_tokens:
                async for ev in self._finish(
                    "budget",
                    answer_parts,
                    surfaced_sources,
                    surfaced_entities,
                    mutated,
                    general_knowledge_label,
                ):
                    yield ev
                return

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                yield ToolCallEvent(id=call.id, name=call.name, arguments=call.arguments)
                # Run the tool while draining any progress it reports into
                # ToolProgressEvents, so a long render (image generation) streams a
                # live preview instead of blocking the turn silently. A sentinel put
                # by the done-callback ends the drain once the tool returns; tools
                # that report nothing just yield no progress (unchanged behaviour).
                task = asyncio.ensure_future(self._dispatch(call, tool_ctx, allowed))
                # A None sentinel (FIFO after every real item) ends the drain.
                task.add_done_callback(lambda _t: live_q.put_nowait(None))
                try:
                    while True:
                        item = await live_q.get()
                        if item is None:
                            break
                        if isinstance(item, tuple):
                            step, total, preview, label = item
                            yield ToolProgressEvent(
                                tool_call_id=call.id,
                                step=step,
                                total=total,
                                preview=preview,
                                label=label,
                            )
                        else:
                            # A whole ChatEvent the handler emitted (subagent_*); anchor it to
                            # the dispatching call so the UI groups it under this tool. Only an
                            # un-anchored event (tool_call_id still the "" default) is stamped,
                            # so a future handler that sets its own id is never clobbered.
                            yield (
                                item.model_copy(update={"tool_call_id": call.id})
                                if getattr(item, "tool_call_id", None) == ""
                                else item
                            )
                    dispatched = await task
                except asyncio.CancelledError:
                    # The turn was cancelled mid-tool (an explicit Stop, or shutdown).
                    # `_dispatch` runs as its OWN task, so the cancellation hitting our await
                    # here does NOT reach it — propagate it explicitly and await the unwind,
                    # so a spawn_subagent fan's children (an inner gather) stop too. Without
                    # this they keep grinding the GPU for minutes after the parent turn ended
                    # — the very runaway a Stop is meant to halt.
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    raise
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                surfaced_sources.extend(dispatched.sources)
                surfaced_entities.extend(dispatched.entities)
                # A staged Proposal, or a tool whose spec declares it mutating, makes
                # the turn critique-worthy — it carried a write, not just a read.
                mutated = mutated or dispatched.proposal is not None or self._is_mutating(call.name)
                yield ToolResultEvent(
                    tool_call_id=call.id,
                    ok=not dispatched.result.is_error,
                    summary=dispatched.result.content,
                    sources=list(dispatched.sources),
                    web_sources=list(dispatched.web_sources),
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
                    "too_many_errors",
                    answer_parts,
                    surfaced_sources,
                    surfaced_entities,
                    mutated,
                    general_knowledge_label,
                ):
                    yield ev
                return

        async for ev in self._finish(
            "max_steps",
            answer_parts,
            surfaced_sources,
            surfaced_entities,
            mutated,
            general_knowledge_label,
        ):
            yield ev

    async def _run_stream_buffered(
        self,
        session: SessionContext,
        scopes: Sequence[str],
        conversation: Sequence[LlmMessage],
        timezone: str | None,
        agent_session_id: str | None = None,
        system: str | None = None,
        tools_allow: frozenset[str] | None = None,
        general_knowledge_label: bool = True,
        here: tuple[float, float] | None = None,
        here_as_of: datetime | None = None,
        context_window: int | None = None,
        depth: int = 0,
        tree: TreeState | None = None,
        run_id: str | None = None,
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
            turn = await self._produce_buffered(
                session,
                scopes,
                conversation,
                timezone,
                budget,
                agent_session_id,
                system,
                tools_allow,
                here,
                here_as_of,
                context_window,
                depth,
                tree,
                run_id,
            )
            corpus = _grounding_corpus(turn.sources, turn.entities)
            cited = len(turn.sources) + len(turn.entities)
            # Empty corpus → grounding is unverifiable, not failed: hand back a clean
            # pass so reflexion neither retries nor flags a turn it cannot judge.
            verdict = (
                aggregate(
                    [verify_grounding(claims_from(turn.answer), corpus, cited_source_count=cited)]
                )
                if corpus
                else VerificationResult(PASS_SCORE, ())
            )
            if incumbent[0] is None:
                incumbent[0] = (turn, verdict)
            return turn, verdict

        first, verdict = await produce()
        if _buffered_critique_worthy(first):
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
        corpus = _grounding_corpus(kept.sources, kept.entities)
        # The same mutually-exclusive tail as `_finish`: an empty corpus + a
        # substantive answer is the neutral general-knowledge label; a non-empty
        # corpus that a critique-worthy turn failed to ground is the amber verdict.
        if not corpus:
            # Only a knowledge-base agent gets the "from general knowledge — not your
            # notes" label; for a non-KB agent (jerv, teacher) there are no notes to
            # contrast with, so the provenance chip is meaningless and is suppressed.
            if general_knowledge_label and has_substantive_claim(kept.answer):
                yield GeneralKnowledgeEvent()
        elif not kept_verdict.passed and _buffered_critique_worthy(kept):
            cited = len(kept.sources) + len(kept.entities)
            yield VerdictEvent(
                passed=False,
                score=kept_verdict.score,
                issues=list(kept_verdict.issues),
                ungrounded_claims=ungrounded_claims(
                    claims_from(kept.answer), corpus, cited_source_count=cited
                ),
            )

    async def _produce_buffered(
        self,
        session: SessionContext,
        scopes: tuple[str, ...],
        conversation: Sequence[LlmMessage],
        timezone: str | None,
        budget: list[int],
        agent_session_id: str | None = None,
        system: str | None = None,
        tools_allow: frozenset[str] | None = None,
        here: tuple[float, float] | None = None,
        here_as_of: datetime | None = None,
        context_window: int | None = None,
        depth: int = 0,
        tree: TreeState | None = None,
        run_id: str | None = None,
    ) -> _BufferedTurn:
        """One full non-streaming produce-step for mode (a): run the turn loop to a
        terminal stop, buffering the ChatEvents it would have streamed (so a
        discarded retry never reaches the user). Shares the remaining cost cap in
        `budget` so retries cannot overspend the per-turn guardrail."""
        system_prompt = system or SYSTEM_PROMPT
        tools = self._registry.schemas_for(scopes, tools_allow)
        allowed = self._registry.allowed_names(scopes, tools_allow)
        messages: list[LlmMessage] = list(conversation)
        tool_ctx = ToolContext(
            session=session,
            scopes=scopes,
            timezone=timezone,
            agent_session_id=agent_session_id,
            here=here,
            here_as_of=here_as_of,
            depth=depth,
            agent_tools=allowed,
            tree=tree,
            run_id=run_id,
        )
        events: list[ChatEvent] = []
        answer_parts: list[str] = []
        sources: list[NoteSource] = []
        entities: list[EntityRef] = []
        mutated = False
        idx = 0
        spent = 0

        for _step in range(self._g.max_steps):
            turn = await self._router.converse(
                self._task,
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=TURN_MAX_TOKENS,
                strength=SYSTEM_STRENGTH,
            )
            spent = turn.usage.input_tokens + turn.usage.output_tokens
            budget[0] -= spent
            if tree is not None:
                tree.charge(spent)
            await self._record(idx, "model", "converse", ok=True, cost_tokens=spent)
            idx += 1
            if context_window is not None:
                events.append(
                    UsageEvent(
                        input_tokens=turn.usage.input_tokens,
                        output_tokens=turn.usage.output_tokens,
                        context_window=context_window,
                    )
                )
            if turn.reasoning:
                # Buffered (non-streaming) twin of the live ReasoningChunk: replay the
                # whole thinking trace before the answer. Never enters answer_parts.
                events.append(ReasoningDelta(text=turn.reasoning))
            if turn.text:
                answer_parts.append(turn.text)
                events.append(TextDelta(text=turn.text))

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                return _BufferedTurn(
                    tuple(events),
                    "".join(answer_parts),
                    tuple(sources),
                    tuple(entities),
                    mutated,
                    "end_turn",
                )
            if self._tree_exhausted(tree, depth):
                return _BufferedTurn(
                    tuple(events),
                    "".join(answer_parts),
                    tuple(sources),
                    tuple(entities),
                    mutated,
                    "tree_budget_exhausted",
                )
            if budget[0] <= 0:
                return _BufferedTurn(
                    tuple(events),
                    "".join(answer_parts),
                    tuple(sources),
                    tuple(entities),
                    mutated,
                    "budget",
                )

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                events.append(ToolCallEvent(id=call.id, name=call.name, arguments=call.arguments))
                dispatched = await self._dispatch(call, tool_ctx, allowed)
                results.append(dispatched.result)
                any_error = any_error or dispatched.result.is_error
                sources.extend(dispatched.sources)
                entities.extend(dispatched.entities)
                mutated = mutated or dispatched.proposal is not None or self._is_mutating(call.name)
                events.append(
                    ToolResultEvent(
                        tool_call_id=call.id,
                        ok=not dispatched.result.is_error,
                        summary=dispatched.result.content,
                        sources=list(dispatched.sources),
                        web_sources=list(dispatched.web_sources),
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
                    tuple(entities),
                    mutated,
                    "too_many_errors",
                )

        return _BufferedTurn(
            tuple(events),
            "".join(answer_parts),
            tuple(sources),
            tuple(entities),
            mutated,
            "max_steps",
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
        entities: list[EntityRef],
        mutated: bool,
        general_knowledge_label: bool = True,
    ) -> AsyncIterator[ChatEvent]:
        """Close the stream: emit the terminal `DoneEvent`, then exactly one of two
        mutually-exclusive tail annotations (or nothing). The answer the user saw
        always stands — no model call, no retry, no persistence.

        - **Zero retrieval, substantive answer →** a neutral `GeneralKnowledgeEvent`:
          the turn answered from the model's own world knowledge (empty grounding
          corpus) with a checkable claim, so we surface calm provenance ("not your
          notes"). This is independent of `critique_worthy` (such a turn is never
          critique-worthy, but we still label it). A greeting / acknowledgement (no
          substantive claim) is left silent. Suppressed entirely when
          `general_knowledge_label` is False — a non-KB agent (jerv, teacher) has no
          notes to contrast with, so the provenance chip would be meaningless.
        - **Retrieval + a critique-worthy turn whose claim failed grounding →** the
          amber `VerdictEvent`. A non-empty corpus that grounds cleanly, or a turn
          that isn't critique-worthy, emits nothing.

        The two can never co-occur: general_knowledge requires an empty corpus, the
        verdict a non-empty one."""
        yield DoneEvent(stop_reason=stop_reason)
        corpus = _grounding_corpus(sources, entities)
        if not corpus:
            # Empty corpus (no note snippets AND no entity texts) → grounding is
            # *unverifiable*, not ungrounded: never an amber flag. But a substantive
            # answer here came purely from the model's own knowledge — label it, unless
            # the agent has no notes to contrast with (a non-KB agent: jerv, teacher).
            if general_knowledge_label and has_substantive_claim("".join(answer_parts)):
                yield GeneralKnowledgeEvent()
            return
        if not critique_worthy(
            source_count=len(sources),
            entity_count=len(entities),
            mutated=mutated,
            touched_sensitive=_touched_sensitive(sources, entities),
        ):
            return
        claims = claims_from("".join(answer_parts))
        # The index space a `[^n]` marker may resolve into: the sources the turn
        # surfaced (notes + entities), in the same order the PWA numbers them.
        cited = len(sources) + len(entities)
        verdict = aggregate([verify_grounding(claims, corpus, cited_source_count=cited)])
        if not verdict.passed:
            yield VerdictEvent(
                passed=False,
                score=verdict.score,
                issues=list(verdict.issues),
                ungrounded_claims=ungrounded_claims(claims, corpus, cited_source_count=cited),
            )

    async def _dispatch(
        self, call: ToolCall, tool_ctx: ToolContext, allowed: frozenset[str]
    ) -> _Dispatched:
        if call.name not in allowed:
            # The allowlist is the dispatch-time boundary, not just a visibility
            # hint: a tool the agent was never offered — a model slip, or a name
            # smuggled in by injected content — is REFUSED here, never run. This is
            # what keeps a knowledge agent (curator) from ever reaching a `web` tool
            # it wasn't granted, even if the model emits the call. Recoverable error,
            # not a crash. `allowed` ⊆ registry, so this also covers unknown names.
            err = ToolResult(
                tool_call_id=call.id, content=f"tool not available: {call.name}", is_error=True
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
            out.web_sources if out else (),
        )

    async def _record(self, idx: int, kind: str, name: str, *, ok: bool, cost_tokens: int) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.step(idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens)
        except Exception as exc:  # noqa: BLE001 — recording must never break a turn
            log.warning("agent.record_failed", error=repr(exc))
