"""The agent turn loop: a thin ReAct cycle over the LLM adapter.

Assemble the conversation, ask the model with the in-scope tools, run any tool
calls it makes, feed the results back, and repeat until it answers or a guardrail
trips. The loop owns the guardrails — step, cost, and consecutive-error caps —
and never trusts the model to stop itself. Tool dispatch and the run record are
the loop's concern; what a tool *does* is the handler's.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

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
# (Raised from 40/30 so a many-source turn — a scheduled news/research Task runs curator
# at medium — rarely truncates mid-chain; the 200k cost-token backstop still bounds a
# runaway.)
STEPS_BY_EFFORT: dict[str, int] = {"high": 60, "medium": 50}

# A SUPERVISED turn — one a foreground PWA client is up watching stream, able to Stop it at
# any moment — earns a much larger per-turn budget: the human is the loop's anchor, so a long
# multi-source web thread or a plan step that legitimately needs many tool calls isn't cut off
# mid-work with "hit the budget" / "too many steps". These are a large FINITE backstop, not a
# truly unbounded loop (the ASSISTANT.md "no unbounded autonomous loop" invariant): the
# consecutive-error cap is untouched and these ceilings still stop a genuinely wedged run. An
# UNsupervised turn — a scheduled background task, or a plan continuation that fired while no
# client was watching — keeps the ordinary effort-sized budget below.
SUPERVISED_MAX_STEPS = 500
SUPERVISED_MAX_COST_TOKENS = 2_000_000

# The forced-final synthesis (force_final_answer, on step/budget/tree exhaustion) writes an
# answer from already-gathered material — a mechanical step that needs no thinking. Run it at
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


def guardrails_for_effort(
    effort: str | None, *, scale: int = 1, supervised: bool = False
) -> Guardrails:
    """The loop's budget sized to the task's effective reasoning effort, then scaled
    by a per-agent factor. `scale` (an agent's `budget_multiplier`, default 1) widens
    BOTH the step cap and the cost-token budget together: the archivist's long, many-
    tool mailbox cleanups run at 4, so a single sweep isn't cut off mid-chain
    (docs/archive/EMAIL_ARCHIVIST_PLAN.md). The consecutive-error cap is unscaled — a wedged
    chain should still bail fast regardless of persona.

    `supervised` lifts the per-turn ceilings to a large finite backstop
    (`SUPERVISED_MAX_*`): a foreground PWA client is up watching this turn stream and can Stop
    it, so the human anchors the loop and a legitimately long turn isn't cut off. The
    supervised ceilings already dominate every persona's scaled default, so `scale` is not
    applied on top of them (JERV_PLANNING_TOOL_PLAN.md)."""
    if supervised:
        return Guardrails(
            max_steps=SUPERVISED_MAX_STEPS,
            max_cost_tokens=SUPERVISED_MAX_COST_TOKENS,
        )
    base = STEPS_BY_EFFORT.get(effort or "", Guardrails.max_steps)
    return Guardrails(
        max_steps=base * scale,
        max_cost_tokens=Guardrails.max_cost_tokens * scale,
    )


@dataclass
class ToolCallBudget:
    """A hard, engine-enforced ceiling on how many times one agent may call a given tool
    this run — the mechanical backstop for a persona whose PROMPT states a budget the model
    then ignores (the deep-research scout: gpt-oss treats a named-source list as a checklist
    and searches once per outlet, then follows lead after lead, running to the step cap
    regardless of the "AT MOST N" wording; MODEL_PROMPTING.md). The tool handler counts each
    call against `used`, refuses the call once `used >= limit`, and annotates every result
    with what's left, so the contract holds no matter the model or reasoning effort. Not
    frozen — `used` is incremented in place; one instance is built per `run` (per child), so
    the scope is exactly this agent's turn. The scout carries two: a tight `web_search` cap
    (its over-search was the first runaway) and a looser `web_fetch` cap (once searches were
    capped the fetch loop became the new long pole — 23 reads in one scout)."""

    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit


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
    # Sub-agent spawning context (docs/archive/SUBAGENT_SPAWNING_PLAN.md). `depth` is this
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
    # Per-turn memo of URLs whose fetch already FAILED this turn (dedup key → HTTP status,
    # 0 when unknown). A tool consults it to refuse re-fetching a known-dead URL instead of
    # burning the budget re-requesting it (esp. a 404 the model keeps reconstructing). The
    # loop reuses one ToolContext for a whole turn, and the default_factory gives each turn
    # its own memo, so the scope is exactly "this run"; only failed fetches ever land here.
    failed_fetches: dict[str, int] = field(default_factory=dict)
    # Hard per-run tool-call ceilings (None = uncapped, the default for every persona but the
    # scout). The web_search / web_fetch handlers count calls against these, refuse once spent,
    # and append the remaining count to each result. Mutated in place (like `failed_fetches`) —
    # the frozen field is the reference, `used` is not frozen.
    search_budget: "ToolCallBudget | None" = None
    fetch_budget: "ToolCallBudget | None" = None


@dataclass(frozen=True)
class JobRef:
    """A job a long/deferred tool enqueued instead of blocking the turn — the id to
    poll and a one-line summary. The loop surfaces it as a `JobEnqueuedEvent`."""

    job_id: str
    summary: str


@dataclass(frozen=True)
class DeferredRef:
    """A tool call that kicked a background job AND ends the turn: the `job_id` to
    cancel, the `result_id` (the run-scoped `media_analysis_results` row) the
    `task_status` card polls for live progress and swaps to on completion, and the chat
    `session_id` the finished result auto-resumes into. Unlike a plain `JobRef` (which
    enqueues but keeps the turn going), a `deferred` result tells the loop to stream the
    tool's `task_status` view and finish the turn (`stop_reason="deferred"`) — the work
    runs off-turn and reports back later (DEFERRED_TOOL_CALLS_PLAN.md P2). One deferred
    call ends the turn; the model does not get another step."""

    job_id: str
    result_id: str
    session_id: str


class ToolOutput(str):
    """A tool observation that also carries what the tool surfaced for the UI —
    note sources (source cards), web sources (favicon citation chips), a staged
    proposal (a "Review proposal" chip), resolved entities, a rich `view` (a
    registered component the PWA renders, e.g. a checklist), a `job` it deferred to
    the queue, and/or a turn-ending `deferred` handle (a background job whose
    `task_status` card takes over — the turn ends). It *is* the model-facing text (a
    str subclass), so handlers keep their `-> str` contract and existing call sites
    are untouched; `_dispatch` pulls the extras off when present."""

    sources: tuple[NoteSource, ...]
    web_sources: tuple[WebSource, ...]
    proposal: ProposalRef | None
    entities: tuple[EntityRef, ...]
    view: ViewPayload | None
    job: JobRef | None
    deferred: DeferredRef | None

    def __new__(
        cls,
        content: str,
        sources: tuple[NoteSource, ...] = (),
        proposal: ProposalRef | None = None,
        entities: tuple[EntityRef, ...] = (),
        view: ViewPayload | None = None,
        job: JobRef | None = None,
        web_sources: tuple[WebSource, ...] = (),
        deferred: DeferredRef | None = None,
    ) -> "ToolOutput":
        out = super().__new__(cls, content)
        out.sources = sources
        out.web_sources = web_sources
        out.proposal = proposal
        out.entities = entities
        out.view = view
        out.job = job
        out.deferred = deferred
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
    # The web pages the run's internet tools reached (the real URLs, captured from the
    # tool calls — never parsed from prose), accumulated across every step. The
    # non-streaming `run()` path aggregates them here so a caller that only sees the
    # AgentResult (a spawned sub-agent) can still recover the sources behind the answer —
    # otherwise a child's citations die at its boundary (deep_research's global registry).
    web_sources: tuple[WebSource, ...] = ()
    # The run's tool steps and reasoning trace, folded into the SAME persisted-transcript
    # shape the streaming parent turn produces (transcript_accumulator.py) — so a spawned
    # child's work can be recorded to its own `agent_turns` row and replay through the
    # identical `fromTurn` path (and be read back in debug SQL). Empty for a run whose caller
    # never persists them; additive, so every existing `AgentResult(...)` call is unaffected.
    tool_steps: tuple[dict[str, Any], ...] = ()
    reasoning: str = ""


# Cap a persisted child tool-step's result text so a run's stored trace stays bounded: a
# research child can read a 60k-char transcript, and persisting that verbatim per step per
# child would balloon the `agent_turns` JSONB. Display/debug only — the model still saw the
# full result in-context this turn; only the STORED copy is capped, with a marker.
_CHILD_STEP_SUMMARY_MAX = 4000


def _persisted_step(
    call: ToolCall, dispatched: "_Dispatched", *, text_offset: int, reasoning_offset: int
) -> dict[str, Any]:
    """One tool call folded into the persisted-transcript step shape — the SAME dict the
    parent's `TranscriptAccumulator` builds (transcript_accumulator.py), so a spawned
    child's steps replay through the identical `fromTurn` hydration and read back in debug
    SQL. The result text is capped (see `_CHILD_STEP_SUMMARY_MAX`); empty/absent surfaced
    data is omitted so a plain search step stays small."""
    summary = dispatched.result.content or ""
    if len(summary) > _CHILD_STEP_SUMMARY_MAX:
        summary = summary[:_CHILD_STEP_SUMMARY_MAX] + "\n[truncated]"
    step: dict[str, Any] = {
        "id": call.id,
        "name": call.name,
        "ok": not dispatched.result.is_error,
        "sources": [s.model_dump() for s in dispatched.sources],
        "text_offset": text_offset,
        "reasoning_offset": reasoning_offset,
        "summary": summary,
    }
    if call.arguments:
        step["args"] = call.arguments
    if dispatched.web_sources:
        step["web_sources"] = [s.model_dump() for s in dispatched.web_sources]
    if dispatched.proposal is not None:
        step["proposal"] = dispatched.proposal.model_dump()
    if dispatched.entities:
        step["entities"] = [e.model_dump() for e in dispatched.entities]
    if dispatched.view is not None:
        step["view"] = dispatched.view.model_dump()
    return step


@dataclass(frozen=True)
class _Dispatched:
    """One tool call's outcome: the result fed back to the model, plus what it
    surfaced for the UI (sources, a staged proposal, entities, a rich view, an
    enqueued job, and/or a turn-ending deferred handle)."""

    result: ToolResult
    sources: tuple[NoteSource, ...]
    proposal: ProposalRef | None
    entities: tuple[EntityRef, ...]
    view: ViewPayload | None
    job: JobRef | None
    web_sources: tuple[WebSource, ...] = ()
    deferred: DeferredRef | None = None


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
        model_override: str | None = None,
        hidden_tools_provider: Callable[[], Awaitable[Collection[str]]] | None = None,
    ):
        self._router = router
        self._registry = registry
        self._recorder = recorder
        self._g = guardrails or Guardrails()
        self._task = task
        # Runtime, per-turn tool exclusion: a backend outage hides the tools that need
        # it (ComfyUI down → the image-gen tools). Awaited once per turn (the provider
        # caches its probe), folded into every schemas_for / allowed_names call.
        self._hidden_tools_provider = hidden_tools_provider
        # A per-conversation model pick (the omnibox long-press sheet): a "provider:model"
        # spec every model call this loop makes runs on, outranking the task's resolved
        # route. None = the resolved default. Scoped to THIS loop (the /chat turn), so a
        # sub-agent the turn spawns still runs on its own configured model.
        self._model_override = model_override

    async def _hidden(self) -> Collection[str]:
        """Tool names hidden this turn by a runtime backend outage (empty when no
        provider is wired or every backend is healthy). Never raises — a probe
        failure must not break a turn, so it degrades to hiding nothing."""
        if self._hidden_tools_provider is None:
            return ()
        try:
            return await self._hidden_tools_provider()
        except Exception:  # noqa: BLE001 — liveness is best-effort; a probe error hides nothing
            log.warning("agent.hidden_tools_probe_failed", exc_info=True)
            return ()

    async def _hide_tool_round_text(self) -> bool:
        """Whether a tool-call round's `content` on THIS route is leaked thinking to hide, not
        the answer. The local gpt-oss harmony route (served via llama.cpp) sometimes emits a
        tool-call round's ANALYSIS on the `content` channel instead of `reasoning_content` — seen
        after a tool result in a multi-tool turn — so that round's "content" is the model's
        reasoning, which then got glued in front of the real reply. Harmony has no Claude-style
        interleaved final text: a tool-call message carries analysis + the call, never a
        user-facing preamble. So for the local route a tool-round's content is ALWAYS reasoning
        and is routed to the thinking trace, never the answer; a hosted model (Claude, Grok) that
        legitimately narrates before a tool call is left untouched. Never raises — a routing
        hiccup degrades to keeping the text (the prior behaviour)."""
        try:
            provider, _model = await self._router.effective_spec(self._task, SYSTEM_STRENGTH)
        except Exception:  # noqa: BLE001 - a routing hiccup must never break a turn
            return False
        return provider == "local"

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
        hide_tool_round_text: bool = False,
    ) -> LlmTurn:
        """One model turn for `run`. With no streaming callbacks it's a plain
        `converse` (the existing non-streaming path, unchanged). With `on_text`/
        `on_reasoning` it streams via `converse_stream` and forwards each chunk to the
        callback as it arrives — the sub-agent spawner uses this to surface a child's
        live tokens — while still returning the closing turn so the loop is identical.

        `hide_tool_round_text` mirrors the root turn's reclassification (`run_stream`):
        on the local route a tool-call round's `content` is the model's leaked thinking,
        not a preamble — so buffer this round's text and, once its stop_reason is known,
        route it to `on_reasoning` (the thinking trace) if the round called a tool, else
        to `on_text` (the round IS the answer). Without this a non-reasoning local model
        like the coder — whose narration arrives on `content`, never a `reasoning_content`
        channel — would show its inter-tool thinking as the child's answer, not as its
        thinking (the gap that made the coder's thinking never reach the fan's trace)."""
        if on_text is None and on_reasoning is None:
            return await self._router.converse(
                self._task,
                system=system_prompt,
                messages=messages,
                tools=tools,  # type: ignore[arg-type]
                max_tokens=TURN_MAX_TOKENS,
                strength=SYSTEM_STRENGTH,
                effort_override=reasoning_effort,
                spec_override=self._model_override,
            )
        turn: LlmTurn | None = None
        # On the local route we can't classify this round's content until its stop_reason
        # arrives (a tool call is signalled only at the end), so buffer the round's text and
        # commit it once we know whether the round is a tool call (thinking) or the answer.
        round_text: list[str] = []
        async for part in self._router.converse_stream(
            self._task,
            system=system_prompt,
            messages=messages,
            tools=tools,  # type: ignore[arg-type]
            max_tokens=TURN_MAX_TOKENS,
            strength=SYSTEM_STRENGTH,
            effort_override=reasoning_effort,
            spec_override=self._model_override,
        ):
            if isinstance(part, TextChunk):
                if part.text:
                    if hide_tool_round_text:
                        round_text.append(part.text)
                    elif on_text is not None:
                        on_text(part.text)
            elif isinstance(part, ReasoningChunk):
                if part.text and on_reasoning is not None:
                    on_reasoning(part.text)
            else:
                turn = part
        # The adapter always closes a stream with an LlmTurn; guard the contract.
        turn = turn or LlmTurn(text="", tool_calls=(), stop_reason="end_turn", usage=LlmUsage(0, 0))
        if hide_tool_round_text and round_text:
            round_content = "".join(round_text)
            if turn.stop_reason == "tool_use" and turn.tool_calls:
                # A tool-call round's content is leaked thinking → surface it as the child's
                # thinking trace, and fold it into the turn's reasoning so the persisted
                # transcript matches (this model has no reasoning_content channel of its own).
                if on_reasoning is not None:
                    on_reasoning(round_content)
                turn = replace(turn, reasoning=turn.reasoning + round_content)
            elif on_text is not None:
                on_text(round_content)
        return turn

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
        # Hard per-run ceilings on web_search / web_fetch calls (None = uncapped). The scout
        # persona passes these so the engine — not the prompt — stops the over-search and the
        # fetch loop (see ToolCallBudget).
        search_budget: int | None = None,
        fetch_budget: int | None = None,
    ) -> AgentResult:
        scopes = tuple(scopes)
        hidden = await self._hidden()
        tools = self._registry.schemas_for(scopes, tools_allow, hidden=hidden)
        allowed = self._registry.allowed_names(scopes, tools_allow, hidden=hidden)
        messages: list[LlmMessage] = list(conversation)
        # `agent_tools=allowed` is this turn's effective ceiling — a child this turn
        # spawns is clamped to it (docs/archive/SUBAGENT_SPAWNING_PLAN.md, the parent⊆child clamp).
        tool_ctx = ToolContext(
            session=session,
            scopes=scopes,
            timezone=timezone,
            agent_session_id=agent_session_id,
            depth=depth,
            agent_tools=allowed,
            tree=tree,
            run_id=run_id,
            search_budget=ToolCallBudget(limit=search_budget) if search_budget else None,
            fetch_budget=ToolCallBudget(limit=fetch_budget) if fetch_budget else None,
        )
        # A caller can swap the system prompt (the wiki Editor uses its own persona); existing
        # callers pass nothing and keep the Full Brain prompt — fully backward-compatible.
        system_prompt = system or SYSTEM_PROMPT
        # The local route leaks a tool-round's thinking onto the content channel; when this
        # streamed run has a reasoning sink, route that content to the thinking trace instead
        # of the answer (the sub-agent twin of run_stream's reclassification). Computed once.
        hide_tool_round_text = on_reasoning is not None and await self._hide_tool_round_text()
        cost = 0
        consecutive_errors = 0
        idx = 0
        # The real URLs the run's internet tools reached, accumulated across steps so the
        # returned AgentResult carries them (a spawned child's only channel out — see the
        # AgentResult.web_sources doc).
        web_sources: list[WebSource] = []
        # The run's persisted tool steps + reasoning, folded into the transcript shape a
        # caller (the spawn service) records to the child's own session so its work replays
        # and is debuggable. Accumulated here so every return path carries them (via
        # `_result`); inert when the caller never persists. `reasoning_parts` tracks the
        # streamed thinking length for each step's interleave offset into the reasoning trace.
        tool_steps: list[dict[str, Any]] = []
        reasoning_parts: list[str] = []

        def _result(text: str, stop_reason: str, step_count: int) -> AgentResult:
            """Every `run` exit builds its AgentResult here so the accumulated tool steps +
            reasoning ride out on all of them, not just the happy path."""
            return AgentResult(
                text,
                stop_reason,
                step_count,
                cost,
                tuple(web_sources),
                tuple(tool_steps),
                "".join(reasoning_parts),
            )

        async def _forced_final(stop_reason: str, step_count: int) -> AgentResult:
            """One final, tool-free synthesis turn so a force_final_answer run (a
            sub-agent) lands on a real answer instead of an empty "(no answer)". Shared
            by EVERY force_final_answer stop — the step cap AND the budget/tree caps,
            which used to early-return the capped turn's (usually empty, mid-tool-call)
            text, so a token-capped child reported nothing (the iTTP cross-check). The
            `stop_reason` is preserved so the caller still sees WHY the child was cut;
            only the answer is guaranteed non-empty. The single no-tool call is a
            bounded post-cap overshoot — the same one the step-cap path already made,
            and exactly what the tree's best-effort root reserve exists to absorb
            (tree.py). Synthesizes from `messages` as they stand (never the dangling
            capped tool-use turn, which was never dispatched), so the conversation stays
            well-formed; FINAL_ANSWER_DIRECTIVE keeps gpt-oss from emitting its next
            search as text instead of synthesizing."""
            nonlocal cost
            final_messages = [*messages, UserMessage(text=FINAL_ANSWER_DIRECTIVE)]
            final = await self._converse_turn(
                system_prompt,
                final_messages,
                (),
                FINAL_ANSWER_EFFORT,
                on_text,
                on_reasoning,
                hide_tool_round_text,
            )
            spent_final = final.usage.input_tokens + final.usage.output_tokens
            cost += spent_final
            if tree is not None:
                tree.charge(spent_final)
            if on_usage is not None:
                on_usage(final.usage.input_tokens, final.usage.output_tokens)
            await self._record(idx, "model", "converse", ok=True, cost_tokens=spent_final)
            reasoning_parts.append(final.reasoning)
            return _result(final.text, stop_reason, step_count)

        for step in range(self._g.max_steps):
            # Soft landing (sub-agents only): a few steps before the hard cap, ask the
            # model to wrap up so it lands on end_turn rather than being force-cut at the
            # cap. Fired once; the forced-final answer below still catches a model that
            # ignores it.
            if force_final_answer and step > 0 and step == self._g.max_steps - _BUDGET_WARNING_LEAD:
                messages.append(UserMessage(text=BUDGET_WARNING_DIRECTIVE))
            turn = await self._converse_turn(
                system_prompt,
                messages,
                tools,
                reasoning_effort,
                on_text,
                on_reasoning,
                hide_tool_round_text,
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
            # Accumulate this turn's thinking so each tool step below records its interleave
            # offset (reasoning length at the moment it was called) and the full reasoning
            # trace persists — the same interleave the streamed parent turn stores.
            reasoning_parts.append(turn.reasoning)
            # Per-step progress hook (Wave S2 follow-up): the spawn service uses it to
            # stream a live subagent_progress per child step so the UI's budget meter
            # and step count move while a child works (children run non-streaming).
            if on_step is not None:
                on_step(step + 1, cost)

            if turn.stop_reason != "tool_use" or not turn.tool_calls:
                # A natural end of turn. But a sub-agent that ends with EMPTY text has
                # produced no answer — gpt-oss occasionally returns an empty final turn (a
                # 0-token completion at high context, or all content stranded in the
                # reasoning channel), which would waste a whole productive ReAct chain as a
                # bare "(no answer; stopped: end_turn)" (the TTP cross-check: 21 tools, then
                # an empty final). Route it through the SAME forced-final synthesis the cap
                # paths use so the child lands on a real answer from what it gathered. Scoped
                # to force_final_answer (sub-agents); the root's empty turn is handled upstream.
                if force_final_answer and not turn.text.strip():
                    return await _forced_final("end_turn", step + 1)
                return _result(turn.text, "end_turn", step + 1)
            if self._tree_exhausted(tree, depth):
                if force_final_answer:
                    return await _forced_final("tree_budget_exhausted", step + 1)
                return _result(turn.text, "tree_budget_exhausted", step + 1)
            if cost >= self._g.max_cost_tokens:
                if force_final_answer:
                    return await _forced_final("budget", step + 1)
                return _result(turn.text, "budget", step + 1)

            messages.append(AssistantMessage(text=turn.text, tool_calls=turn.tool_calls))
            results: list[ToolResult] = []
            any_error = False
            for call in turn.tool_calls:
                dispatched = await self._dispatch(call, tool_ctx, allowed)
                results.append(dispatched.result)
                web_sources.extend(dispatched.web_sources)
                any_error = any_error or dispatched.result.is_error
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                # Fold the call into the persisted step shape so a caller can record the
                # child's work to its own transcript and it's debuggable; the live `on_tool`
                # forward below is unchanged. `reasoning_offset` indexes the full persisted
                # reasoning trace (exact). `text_offset` is 0: the caller persists the child's
                # FINAL answer as `content`, and every tool call happens in an earlier ReAct
                # turn — before any of that answer is emitted — so each step's split point in
                # the shown answer is its start.
                tool_steps.append(
                    _persisted_step(
                        call,
                        dispatched,
                        text_offset=0,
                        reasoning_offset=sum(len(p) for p in reasoning_parts),
                    )
                )
                # Surface the tool step to a caller streaming the run (the sub-agent fan's
                # live "Worked" list); the args go too so it can show what was searched.
                if on_tool is not None:
                    on_tool(call.name, call.arguments, not dispatched.result.is_error)
                idx += 1
            messages.append(ToolResultMessage(results=results))

            consecutive_errors = consecutive_errors + 1 if any_error else 0
            if consecutive_errors >= self._g.max_consecutive_tool_errors:
                return _result(turn.text, "too_many_errors", step + 1)

        if force_final_answer:
            # Out of steps mid-chain — synthesize from what's gathered rather than
            # reporting nothing. Still flagged `max_steps` so the caller knows why.
            return await _forced_final("max_steps", self._g.max_steps)
        return _result("", "max_steps", self._g.max_steps)

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
        extra_tools: frozenset[str] = frozenset(),
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

        Reflexion (Loop 1, docs/reference/ASSISTANT.md) rides at the tail: the loop tracks
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
                extra_tools,
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
        # (docs/reference/ASSISTANT.md "Agent selection"); the default is the Full Brain curator.
        system_prompt = system or SYSTEM_PROMPT
        hidden = await self._hidden()
        tools = self._registry.schemas_for(scopes, tools_allow, extra_tools, hidden)
        allowed = self._registry.allowed_names(scopes, tools_allow, extra_tools, hidden)
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
        # The local gpt-oss route leaks a tool-round's analysis onto the content channel; buffer
        # that round's text and route it to the thinking trace instead of the answer (see
        # `_hide_tool_round_text`). A hosted model keeps its live per-chunk stream byte-for-byte.
        hide_tool_round_text = await self._hide_tool_round_text()

        for _step in range(self._g.max_steps):
            turn: LlmTurn | None = None
            # On the local route we can't classify this round's content until its stop_reason
            # arrives (a tool call is signalled only at the end), so buffer the round's text and
            # commit it once we know whether the round is a tool call (hide) or the answer (show).
            round_text: list[str] = []
            async for part in self._router.converse_stream(
                self._task,
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=TURN_MAX_TOKENS,
                strength=SYSTEM_STRENGTH,
                spec_override=self._model_override,
            ):
                if isinstance(part, TextChunk):
                    if part.text:
                        if hide_tool_round_text:
                            round_text.append(part.text)
                        else:
                            answer_parts.append(part.text)
                            yield TextDelta(text=part.text)
                elif isinstance(part, ReasoningChunk):
                    # The model's thinking trace — streamed to the PWA's "thinking"
                    # disclosure, never added to the answer or the grounding corpus.
                    if part.text:
                        yield ReasoningDelta(text=part.text)
                else:
                    turn = part
            if turn is not None and hide_tool_round_text and round_text:
                # Commit the buffered round now that its stop_reason is known: a tool-call round's
                # content is leaked harmony analysis → show it as thinking, never the answer; the
                # final (non-tool) round's content IS the answer.
                round_content = "".join(round_text)
                if turn.stop_reason == "tool_use" and turn.tool_calls:
                    yield ReasoningDelta(text=round_content)
                else:
                    answer_parts.append(round_content)
                    yield TextDelta(text=round_content)
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
            deferred_seen: DeferredRef | None = None
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
                if dispatched.deferred is not None:
                    deferred_seen = dispatched.deferred
                await self._record(
                    idx, "tool", call.name, ok=not dispatched.result.is_error, cost_tokens=0
                )
                idx += 1
            messages.append(ToolResultMessage(results=results))

            if deferred_seen is not None:
                # A tool kicked a background job and streamed its task_status card, which
                # now owns the turn: end here (do NOT call the model again) — the job runs
                # off-turn under its own cancel-safe worker, and its result auto-resumes
                # into the chat as a follow-up turn (DEFERRED_TOOL_CALLS_PLAN.md P2/P3).
                async for ev in self._finish(
                    "deferred",
                    answer_parts,
                    surfaced_sources,
                    surfaced_entities,
                    mutated,
                    general_knowledge_label,
                ):
                    yield ev
                return

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
        extra_tools: frozenset[str] = frozenset(),
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
                extra_tools,
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
        extra_tools: frozenset[str] = frozenset(),
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
        hidden = await self._hidden()
        tools = self._registry.schemas_for(scopes, tools_allow, extra_tools, hidden)
        allowed = self._registry.allowed_names(scopes, tools_allow, extra_tools, hidden)
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
        # Local gpt-oss route: a tool-call round's content is leaked harmony analysis, not the
        # answer — route it to the thinking trace (see `_hide_tool_round_text`).
        hide_tool_round_text = await self._hide_tool_round_text()

        for _step in range(self._g.max_steps):
            turn = await self._router.converse(
                self._task,
                system=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=TURN_MAX_TOKENS,
                strength=SYSTEM_STRENGTH,
                spec_override=self._model_override,
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
                if hide_tool_round_text and turn.stop_reason == "tool_use" and turn.tool_calls:
                    # A tool-call round's content on the local route is leaked analysis — replay
                    # it as thinking, never glue it in front of the real answer.
                    events.append(ReasoningDelta(text=turn.text))
                else:
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
        except Exception as exc:  # noqa: BLE001 — a tool error is an observation, not a crash
            # A raised exception becomes a recoverable observation (CancelledError is a
            # BaseException, so a Stop still propagates). The model gets a generic, ACTIONABLE
            # message — not the raw exception string (a dead-end that can leak internals); the
            # detail stays in the log.
            log.warning("agent.tool_error", tool=call.name, error=repr(exc))
            # If the tool authored call examples, echo the first one — a raised exception is
            # often a malformed-args call, so showing the exact shape is self-teaching.
            hint = ""
            if tool.toolfile.examples:
                hint = f" Example call: {json.dumps(tool.toolfile.examples[0], ensure_ascii=False)}"
            base = (
                f"{call.name} hit an internal error and did not run. Try a different approach or"
                " another tool; if it keeps failing, tell the owner what you attempted."
            )
            err = ToolResult(tool_call_id=call.id, content=base + hint, is_error=True)
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
            out.deferred if out else None,
        )

    async def _record(self, idx: int, kind: str, name: str, *, ok: bool, cost_tokens: int) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder.step(idx=idx, kind=kind, name=name, ok=ok, cost_tokens=cost_tokens)
        except Exception as exc:  # noqa: BLE001 — recording must never break a turn
            log.warning("agent.record_failed", error=repr(exc))
