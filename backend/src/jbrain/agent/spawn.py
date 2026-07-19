"""The sub-agent spawn service (docs/archive/SUBAGENT_SPAWNING_PLAN.md, Wave S1).

Only `jerv` (the root turn, depth 0) calls the `spawn_subagent` tool; its handler is a
`SpawnRef` that forwards to `SpawnService.spawn_fan`. The service launches a **fan** of
web-sandboxed children as in-request `asyncio.gather` tasks the parent turn awaits
(fan-in model A), collects their summaries in stable label order, and returns them as a
single observation the parent then synthesizes. Children are always **leaves** —
child-initiated nesting was removed; `waves` (feeding waves) is the orchestrator-declared
way to make one child build on another.

Every safety property here is structural — enforced with no model cooperation:

- **Persona validation** against the closed `SUBAGENT_PERSONAS` set BEFORE
  `agent_for` (which falls back to the KB-capable curator on an unknown name).
- **Parent⊆child clamp:** a child's effective tools = `persona.tools ∩
  parent.agent_tools`, passed as the child loop's `tools_allow` and refused at
  dispatch. Child read scope is empty (jerv-only-root → no domain data, ever).
- **Leaf children:** a child holds no `spawn_subagent` (persona allowlists) and is
  refused by the depth cap anyway — the tree is exactly two levels.
- **Fan/tree caps:** per-fan size, the tree-wide total, and concurrency.
- **Sandbox:** each child session is `no_memory` (and the helper never records an
  episode), and its `ToolContext.here`/`here_as_of` are None (no location).
- **Feed boundary:** a fed consumer's brief is template-bound and its upstream data is
  wrapped in the data/instruction boundary (feeding waves) — never free-text prose.

A refused or failed spawn is a structured observation (never an exception) so the
model self-corrects; a child that errors degrades to an error summary and the rest
of the fan proceeds. A cancelled parent turn cascades `CancelledError` into the
gathered children.
"""

import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

import structlog

from jbrain.agent.agents import SUBAGENT_PERSONAS, agent_for
from jbrain.agent.briefs import BriefError, compose_feed_block, prepend_feed, render_brief
from jbrain.agent.clock import now_block
from jbrain.agent.contracts import (
    ChatEvent,
    SubagentDeltaEvent,
    SubagentDoneEvent,
    SubagentProgressEvent,
    SubagentSpawnedEvent,
    SubagentToolEvent,
    SubagentUsageEvent,
    ToolViewEvent,
    ViewPayload,
    WebSource,
)
from jbrain.agent.loop import AgentLoop, Guardrails, ToolContext, ToolOutput
from jbrain.agent.runlog import AgentRunLog, StepTally
from jbrain.agent.session import AgentSessionInfo, AgentSessionRepo, read_context
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.agent.tree import (
    CHILD_MAX_COST_TOKENS,
    CHILD_WALL_CLOCK_S,
    MAX_CHILDREN_PER_PARENT,
    MAX_DEPTH,
    MAX_PARALLEL,
    MAX_WAVES,
    MIN_VIABLE_CHILD_BUDGET,
    TreeState,
    child_steps_for,
)
from jbrain.db.session import SessionContext
from jbrain.llm import LlmRouter, UserMessage
from jbrain.llm.providers import REASONING_EFFORTS

log = structlog.get_logger(__name__)

_TITLE_LEN = 120  # a child session title is a short label; longer is clamped
# The LLM task a child loop runs on (mirrors AgentLoop's default) — consulted to
# detect a local route, which serializes the fan (see _effective_max_parallel).
_CHILD_TASK = "agent.turn"
# The working word each persona shows while running (the live status word; a neutral
# tag carries the persona itself — see DESIGN.md "Sub-agent spawning surfaces").
_PHASE = {"research": "researching", "review": "reviewing", "summarize": "summarizing"}


def _emit(ctx: ToolContext, event: ChatEvent) -> None:
    """Push a live `subagent_*` event onto the parent turn's stream, if this turn has
    an event sink (only the streaming root turn does — children are leaves and never
    spawn, so there is no nested fan to surface; fan-in model A)."""
    if ctx.emit_event is not None:
        ctx.emit_event(event)


@dataclass(frozen=True)
class _ChildPlan:
    persona: str
    label: str
    brief_text: str
    # The spawner's chosen reasoning effort for this child (None → the child model's
    # resolved default; ignored by the router for a non-reasoning model).
    effort: str | None = None


@dataclass(frozen=True)
class _ChildResult:
    label: str
    persona: str
    summary: str
    ok: bool
    # The child's own session id (childId) — carried into the synthesis view so the
    # roster card can deep-link each row to the sub-agent's session on reopen. Empty
    # for a skipped consumer (it never ran, so no session was minted — the review's
    # anti-orphan requirement, docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md).
    session_id: str
    truncated: bool = False
    # A consumer that never ran because its fed producer was unavailable: the reason
    # ("upstream <label> failed"). Empty for a child that actually ran. A skipped child
    # is never `ok` and is surfaced distinctly from a failure (it is a cascade, not a
    # crash) — so the parent never synthesizes over an empty block.
    skipped: str = ""
    # Staged-fan placement (feeding waves): the child's wave (0-based; 0 for a flat fan)
    # and the earlier-wave producer labels fed into it — so the synthesis card can group
    # by wave and draw the "← fed by …" edge (F3). Empty/0 for a flat fan.
    wave: int = 0
    fed_from: tuple[str, ...] = ()
    # The web pages this child's internet tools actually reached (the real URLs, captured
    # from the tool calls — never parsed from prose). Carried up so a caller like
    # deep_research can build a GLOBAL citation registry: the child's `[^n]` markers are
    # local and die at this boundary otherwise, so without this the URLs behind a fan's
    # findings are lost and the final report can't render tappable favicon citations.
    web_sources: tuple[WebSource, ...] = ()


def effective_child_tools(
    persona_tools: frozenset[str] | None, parent_tools: frozenset[str]
) -> frozenset[str]:
    """The parent⊆child clamp: a child holds at most its persona's allowlist
    intersected with the parent's effective tools — never more than the parent, even
    if the persona lists a tool the parent lacks. Passed as the child loop's
    `tools_allow` and re-enforced at dispatch."""
    return (persona_tools or frozenset()) & parent_tools


# Mirrors the frontend's INLINE_ARG_KEY (FullBrainSurface.tsx): the one arg worth
# previewing per tool — a query/url/name/place/subject, never an opaque id.
_TOOL_ARG_KEY = {
    "search": "query",
    "recall": "query",
    "web_search": "query",
    "web_fetch": "url",
    "gmail_search": "query",
    "gmail_count": "query",
    "gmail_bulk_label": "query",
    "gmail_sender_breakdown": "query",
    "find_entity": "name",
    "lookup_medication": "name",
    "lookup_condition": "name",
    "relate": "relationship",
    "find_when_at": "place",
    "time_at_place": "place",
    "location_query": "place",
    "where_is": "subject",
    "weather": "location",
    "hurricane": "location",
}
_TOOL_ARG_LEN = 200  # a child tool step's inline preview is short; longer is clamped


def _tool_arg(name: str, args: object) -> str:
    """A short inline preview of a child tool call for the fan's Worked list — the
    searched query, fetched url, or looked-up target, like the main step rows.
    Empty for tools whose only args are opaque ids."""
    key = _TOOL_ARG_KEY.get(name)
    if key and isinstance(args, dict):
        raw = args.get(key)
        if isinstance(raw, str):
            return raw.strip()[:_TOOL_ARG_LEN]
    return ""


def _refuse(reason: str) -> str:
    """A refused spawn is a normal observation (is_error=False so it does not trip
    the consecutive-error guardrail) the model can read and act on."""
    return f"Spawn refused: {reason}"


def _free_text_brief(brief: object) -> str:
    """A free-text (string) brief — the depth-0 / un-fed form. Fail closed on a
    non-string or empty brief."""
    if not isinstance(brief, str):
        raise BriefError("this brief must be free text (a string)")
    if not brief.strip():
        raise BriefError("the brief is empty")
    return brief


def _template_brief(brief: object) -> str:
    """A template-bound `{template_id, params}` brief — the form a FED consumer must use
    (feeding waves), whose slots frame every value as data so untrusted upstream output
    cannot become instruction. Fail closed on any other shape."""
    if not isinstance(brief, dict):
        raise BriefError("this brief must be template-bound ({template_id, params}), not free text")
    template_id = brief.get("template_id")
    params = brief.get("params")
    if not isinstance(template_id, str) or not isinstance(params, dict):
        raise BriefError("a template-bound brief needs template_id (str) and params (object)")
    return render_brief(template_id, params)


# --- Feeding waves: plan validation (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md) ----


@dataclass(frozen=True)
class _WavePlan:
    """One validated consumer/producer in a staged fan: its persona, label, resolved
    base brief (before the feed block is prepended at run time), the labels it feeds
    from (all in strictly earlier waves), and its reasoning effort."""

    persona: str
    label: str
    base_brief: str
    feed: tuple[str, ...]
    effort: str | None = None


# Generic cross-task references a brief must not make without a `feed` edge — the
# observed foot-gun (a consumer that says "the same commit list" but never received
# it). Matched case-insensitively as substrings; a brief that names another task's
# label is caught separately by a word-boundary check.
_GUARD_PHRASES = (
    "the same list",
    "the same commit list",
    "provided below",
    "provided above",
    "the earlier findings",
    "per the first agent",
    "from the previous",
    "results from wave",
    "the above output",
    "the output of task",
    "given the json",
    "given the commit list",
)


def _names_unfed_sibling(
    brief: str, self_label: str, all_labels: frozenset[str], feed: frozenset[str]
) -> str | None:
    """Return another task's label if this brief names it but does not `feed` from it —
    the reference is to data the child will never receive. Applies to EVERY task, fed or
    not: a fed consumer that pulls `alpha` but whose brief also says "combine with beta"
    would otherwise run empty against beta (the guard's fed-consumer bypass, caught in
    the F1 review). Labels are matched on word boundaries and regex-escaped."""
    for label in all_labels:
        if label == self_label or label in feed:
            continue
        if re.search(rf"\b{re.escape(label)}\b", brief, re.IGNORECASE):
            return label
    return None


def _references_missing_data(brief: str) -> str | None:
    """Return a guard phrase if an UN-FED brief refers to data it never received ("the
    same commit list", "provided above"). Only meaningful for an un-fed task — a fed
    consumer legitimately refers to the feed block prepended to its brief, so running
    these phrase checks on it would false-positive on the normal case."""
    low = brief.lower()
    for phrase in _GUARD_PHRASES:
        if phrase in low:
            return phrase
    return None


def _resolve_wave_brief(brief: object, *, fed: bool) -> str:
    """The brief a staged child receives. A **fed** consumer MUST be template-bound
    EVEN at depth 0 — an explicit branch, because a fed brief carries untrusted upstream
    output and the template form frames every slot as data (the review's depth-0
    concern). An **un-fed** producer may be free text."""
    return _template_brief(brief) if fed else _free_text_brief(brief)


def plan_waves(waves: list) -> tuple[list[list[_WavePlan]], str | None]:
    """Validate a staged `waves` array up front and build per-wave plans, or return a
    refusal string on the first problem (fail-closed, like the flat fan). Enforces:
    unique labels; `feed` references only strictly-earlier-wave labels; fed ⇒
    template-bound; and the un-fed sibling-reference guard. Returns `([], reason)` on
    refusal, `(plans, None)` on success."""
    if not all(isinstance(w, list) and w for w in waves):
        return [], "each wave must be a non-empty list of children."
    # Pass 1: collect every label (with its wave) and reject duplicates — feed
    # references must be unambiguous across the whole call.
    label_wave: dict[str, int] = {}
    for w_idx, wave in enumerate(waves):
        for task in wave:
            if not isinstance(task, dict):
                return [], f"wave {w_idx}: every child must be an object."
            label = task.get("label")
            if not isinstance(label, str) or not label.strip():
                return [], f"wave {w_idx}: every child needs a non-empty `label`."
            if label in label_wave:
                return [], f"duplicate label {label!r}; labels must be unique across all waves."
            label_wave[label] = w_idx
    all_labels = frozenset(label_wave)
    # Pass 2: validate persona, feed edges, brief, effort, and the guard.
    plans: list[list[_WavePlan]] = []
    for w_idx, wave in enumerate(waves):
        wave_plans: list[_WavePlan] = []
        for task in wave:
            label = task["label"]
            persona = task.get("persona")
            if persona not in SUBAGENT_PERSONAS:
                return [], (
                    f"{label}: unknown persona {persona!r}; "
                    f"choose one of {sorted(SUBAGENT_PERSONAS)}."
                )
            feed_raw = task.get("feed", [])
            if not isinstance(feed_raw, list) or not all(isinstance(f, str) for f in feed_raw):
                return [], f"{label}: `feed` must be a list of labels."
            feed = tuple(feed_raw)
            for f in feed:
                if f not in label_wave:
                    return [], f"{label}: feed references unknown label {f!r}."
                if label_wave[f] >= w_idx:
                    return [], (
                        f"{label}: feed references {f!r}, which is not in an earlier wave "
                        "(a consumer may only feed from a strictly earlier wave)."
                    )
            raw_brief = task.get("brief")
            try:
                base_brief = _resolve_wave_brief(raw_brief, fed=bool(feed))
            except BriefError as exc:
                return [], f"{label}: {exc}"
            # The guard scans only MODEL-SUPPLIED text — a fed consumer's template
            # PARAM VALUES, or an un-fed task's free-text brief — never the fixed
            # template scaffolding (whose words like "summary"/"artifact" would collide
            # with a producer that happens to share that label). F2 review fix.
            if feed and isinstance(raw_brief, dict) and isinstance(raw_brief.get("params"), dict):
                guard_text = " ".join(str(v) for v in raw_brief["params"].values())
            else:
                guard_text = base_brief
            # Naming another task's label without feeding it is a problem for ANY task
            # (a fed consumer can still reference a second, un-fed sibling). The guidance
            # is wave-aware: a same/later-wave sibling cannot be fed, so it must move.
            named = _names_unfed_sibling(guard_text, label, all_labels, frozenset(feed))
            if named is not None:
                if label_wave[named] < w_idx:
                    return [], (
                        f"{label}: the brief refers to {named!r} but does not `feed` from it — "
                        "add a `feed` edge to that producer (or inline the data) so it is received."
                    )
                return [], (
                    f"{label}: the brief refers to {named!r}, which is in the same or a later wave "
                    f"and cannot be fed — move {named!r} to an earlier wave and `feed` from it."
                )
            # Generic "the data provided above" phrasing only refuses an UN-FED task (a
            # fed consumer legitimately refers to its prepended feed block).
            if not feed:
                phrase = _references_missing_data(base_brief)
                if phrase is not None:
                    return [], (
                        f"{label}: the brief refers to {phrase!r} but declares no `feed` — add a "
                        "`feed` edge to the producer (or inline the data) so the child receives it."
                    )
            effort = task.get("effort")
            if effort is not None and effort not in REASONING_EFFORTS:
                return [], (
                    f"{label}: unknown effort {effort!r}; "
                    f"choose one of {sorted(REASONING_EFFORTS)}."
                )
            wave_plans.append(_WavePlan(persona, label, base_brief, feed, effort))
        plans.append(wave_plans)
    return plans, None


class SpawnService:
    """Launches and awaits a fan of web-sandboxed children, reusing the agent
    building blocks (the loop, the session repo, the run log) — not the scheduler's
    TaskRunner (which is neither concurrent nor awaited in-request)."""

    def __init__(
        self,
        *,
        router: LlmRouter,
        registry: ToolRegistry,
        sessions: AgentSessionRepo,
        runlog: AgentRunLog,
        transcript: AgentTranscript | None = None,
    ) -> None:
        self._router = router
        self._registry = registry
        self._sessions = sessions
        self._runlog = runlog
        # Optional: when present, each child's brief→result is persisted to its own
        # session transcript so opening the child in the sessions rail replays its work
        # (separate from episodic memory — `no_memory` gates recall, not this display).
        self._transcript = transcript

    async def spawn_fan(self, ctx: ToolContext, args: dict) -> str:
        """Dispatch a spawn call to the flat fan (an ordinary `tasks` array) or the
        staged wave scheduler (an ordered `waves` array that feeds each wave forward).
        A call with no `waves` takes the byte-identical flat path — the feeding feature
        is purely additive (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md)."""
        if args.get("waves") is not None:
            return await self._spawn_waves(ctx, args)
        return await self._spawn_flat(ctx, args)

    async def _spawn_flat(self, ctx: ToolContext, args: dict) -> str:
        # --- fail closed without an established tree pool ---------------------
        # The tree counter is the load-bearing total-agents cap (decision #8); it
        # exists only when a root turn seeded one (the interactive /chat turn does,
        # api/agent.py). A caller that never threaded a tree — e.g. the scheduled
        # task runner — must NOT be able to spawn an unbounded fan, so spawning is
        # refused rather than counted against a throwaway counter. This also keeps
        # spawn an owner-initiated-turn action (decision #10).
        if ctx.tree is None:
            return _refuse("sub-agent spawning is only available in an interactive owner turn.")
        # --- depth cap (structural, no model cooperation) ---------------------
        # Only the root turn (jerv, depth 0) may spawn; a child is always a leaf. Belt
        # and suspenders with the persona allowlists, which no longer offer a child the
        # spawn tool at all.
        if ctx.depth >= MAX_DEPTH:
            return _refuse("a sub-agent cannot spawn its own sub-agents; only jerv fans out.")

        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return _refuse("provide a non-empty `tasks` array (one entry per child).")
        if len(tasks) > MAX_CHILDREN_PER_PARENT:
            return _refuse(
                f"{len(tasks)} children requested; a single fan may launch at most "
                f"{MAX_CHILDREN_PER_PARENT}."
            )

        # --- validate every child up front (persona + brief + effort), reject the
        #     fan on the first bad one so a malformed/injected persona never resolves -
        plans: list[_ChildPlan] = []
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                return _refuse(f"task {i} is not an object.")
            persona = task.get("persona")
            label = task.get("label")
            if persona not in SUBAGENT_PERSONAS:
                return _refuse(
                    f"unknown persona {persona!r}; choose one of {sorted(SUBAGENT_PERSONAS)}."
                )
            if not isinstance(label, str) or not label.strip():
                return _refuse(f"task {i} needs a non-empty `label`.")
            try:
                # Only jerv (depth 0) reaches here, so a flat-fan brief is always free
                # text (children are leaves; feeding waves handle the template form).
                brief_text = _free_text_brief(task.get("brief"))
            except BriefError as exc:
                return _refuse(f"task {i} ({label}): {exc}")
            # Optional per-child reasoning effort the spawner picks (how hard a
            # reasoning-capable child model thinks; ignored for a non-reasoning model
            # by the router). Absent → None, the child's resolved default.
            effort = task.get("effort")
            if effort is not None and effort not in REASONING_EFFORTS:
                return _refuse(
                    f"task {i} ({label}): unknown effort {effort!r}; "
                    f"choose one of {sorted(REASONING_EFFORTS)}."
                )
            plans.append(_ChildPlan(persona, label, brief_text, effort))

        # --- tree-wide total cap ---------------------------------------------
        tree = ctx.tree  # never None here (guarded above)
        if not tree.can_admit(len(plans)):
            return _refuse(
                f"this fan of {len(plans)} would exceed the tree limit of "
                f"{tree.max_total_agents} sub-agents ({tree.agents_spawned} already running)."
            )
        # --- budget admission floor (Wave S2) --------------------------------
        # Refuse rather than launch children too small to be useful: the children's
        # pool (tree budget minus the root's synthesis reserve, minus all spend so
        # far) must cover a minimum viable slice for each child in the fan.
        if not tree.can_admit_budget(len(plans)):
            return _refuse(
                f"the remaining sub-agent budget (~{tree.children_remaining()} tokens) is too low "
                f"to launch {len(plans)} children; narrow the fan or let the current work finish."
            )
        tree.admit(len(plans))

        max_parallel = await self._effective_max_parallel(args.get("max_parallel"))
        results = await self._execute_fan(ctx, tree, plans, max_parallel=max_parallel)
        # The text observation is what the parent synthesizes from; the view is the
        # UI's structured render of the same fan result (the registered
        # `subagent_synthesis` tool-view, DESIGN.md). Both carry the same data.
        return ToolOutput(_observation(results), view=_synthesis_view(results))

    async def _execute_fan(
        self,
        ctx: ToolContext,
        tree: TreeState,
        plans: list[_ChildPlan],
        *,
        max_parallel: int,
        emit_view: bool = True,
    ) -> list[_ChildResult]:
        """Mint, launch, and await one flat fan of pre-validated, already-admitted
        plans; return the structured results in plan order. This is the single fan
        execution path — shared by the `spawn_subagent` flat fan and by
        `deep_research`'s gather/refill rounds — so the parent⊆child clamp, the
        `no_memory` / no-location sandbox, and the lineage all apply identically no
        matter who launches the fan. The caller owns validation, admission
        (`tree.admit`), and folding the results into an observation/view.

        `emit_view=False` suppresses the per-settle `subagent_synthesis` roster view:
        `deep_research` runs several internal fans and emits its OWN `deep_research_report`
        view, so the fan's roster card must not persist a second, competing card (it would
        settle to the last internal fan — the critique — and misrepresent the run). The
        live `subagent_spawned`/`_done` rows still fire, so the agents are still visible."""
        owner_ctx = SessionContext(principal_id=ctx.session.principal_id, principal_kind="owner")
        sem = asyncio.Semaphore(max_parallel)

        # Mint and announce EVERY child up front (before the semaphore gates execution),
        # so the whole roster shows immediately — the not-yet-started ones as "queued" —
        # even when the fan runs serially. Each child flips to its working phase only when
        # it actually starts (inside _run_child's semaphore block).
        minted: list[tuple[_ChildPlan, AgentSessionInfo]] = []
        for plan in plans:
            child = await self._sessions.create(
                owner_ctx,
                domain_scopes=[],
                title=plan.label[:_TITLE_LEN],
                agent=plan.persona,
                parent_session_id=ctx.agent_session_id,
                depth=ctx.depth + 1,
                no_memory=True,
            )
            _emit(
                ctx,
                SubagentSpawnedEvent(
                    child_id=child.id, persona=plan.persona, label=plan.label, depth=ctx.depth + 1
                ),
            )
            minted.append((plan, child))

        # Collect results in PLAN order as each child settles, and re-emit the
        # roster-so-far as the spawn step's view at every settle. A fan cut short (Stop,
        # error, or turn_timeout) never reaches the final `ToolOutput` view below — so
        # without this, the spawn step persists with NO view and the whole research
        # surface vanishes on reload. The loop stamps the spawn call id (tool_call_id="");
        # the live UI suppresses this view under the live fan, so it only ever surfaces on
        # a reopened transcript — the children that had finished when the turn was cut.
        collected: list[_ChildResult | None] = [None] * len(minted)

        async def _run_and_collect(
            i: int, plan: _ChildPlan, child: AgentSessionInfo
        ) -> _ChildResult:
            res = await self._run_child(ctx, owner_ctx, tree, sem, plan, child)
            collected[i] = res
            if emit_view:
                settled = [r for r in collected if r is not None]
                _emit(ctx, ToolViewEvent(tool_call_id="", view=_synthesis_view(settled)))
            return res

        # asyncio.gather, when this task is cancelled (a Stop / shutdown), cancels every
        # child AND awaits its cancellation cleanup before propagating — the
        # `_GatheringFuture` resolves only once all children are done — so each child's
        # run-log close (status=cancelled) lands inline, not stranded "running". Paired
        # with the loop cancelling this dispatched fan (loop.py), a Stop tears the whole
        # tree down cleanly (test_cancelled_child_runlog_settles_inline_not_detached).
        return await asyncio.gather(
            *(_run_and_collect(i, plan, child) for i, (plan, child) in enumerate(minted))
        )

    async def run_research_fan(
        self,
        ctx: ToolContext,
        *,
        briefs: Sequence[tuple[str, str]],
        persona: str = "research",
        effort: str | None = None,
        max_parallel: int | None = None,
    ) -> list[_ChildResult]:
        """Run one flat fan of `persona` children for `deep_research`'s gather/refill
        rounds and return the structured results (no observation/view fold — the caller
        composes the report). `briefs` is `(label, brief_text)` per child.

        Enforces the SAME admission and sandbox as the `spawn_subagent` flat fan by
        going through `_execute_fan`; the caller (`deep_research`) is responsible for
        the depth/tree guards and for keeping total children across its rounds within
        `MAX_CHILDREN_PER_PARENT`. Returns `[]` when the fan cannot be admitted (tree
        total or budget floor), so the caller renders a coverage-limited report rather
        than failing — a refused refill is not a crash."""
        tree = ctx.tree
        if tree is None or not briefs:
            return []
        plans = [_ChildPlan(persona, label, brief, effort) for label, brief in briefs]
        if not tree.can_admit(len(plans)) or not tree.can_admit_budget(len(plans)):
            return []
        tree.admit(len(plans))
        n = await self._effective_max_parallel(max_parallel)
        # emit_view=False: deep_research emits its own deep_research_report view, so the
        # internal fans must not persist a competing subagent_synthesis roster card.
        return await self._execute_fan(ctx, tree, plans, max_parallel=n, emit_view=False)

    async def _spawn_waves(self, ctx: ToolContext, args: dict) -> str:
        """Run an ordered sequence of disconnected waves, feeding each wave's summaries
        forward into the next wave's briefs (docs/archive/SUBAGENT_FEEDING_WAVES_PLAN.md). Each
        wave is a flat fan; a hard barrier (the awaited gather) separates them. Children
        are minted/admitted per wave, so a skipped or never-reached wave orphans nothing.
        A consumer whose fed producer failed is skipped, never run over empty data."""
        if ctx.tree is None:
            return _refuse("sub-agent spawning is only available in an interactive owner turn.")
        # No nesting (decision D4): staged waves are a top-level capability; a nested
        # child spawns a flat fan. Keeps the feed hop single and the surface bounded.
        if ctx.depth != 0:
            return _refuse(
                "staged `waves` are a top-level capability; a nested sub-agent spawns a flat fan."
            )

        if args.get("tasks") is not None:
            return _refuse(
                "provide either `tasks` (a flat fan) or `waves` (a staged pipeline), not both."
            )

        waves = args.get("waves")
        if not isinstance(waves, list) or not waves:
            return _refuse("provide a non-empty `waves` array (each wave a list of children).")
        if len(waves) > MAX_WAVES:
            return _refuse(
                f"{len(waves)} waves requested; a staged fan may chain at most {MAX_WAVES}."
            )

        wave_plans, refusal = plan_waves(waves)
        if refusal is not None:
            return _refuse(refusal)
        total = sum(len(w) for w in wave_plans)
        if total > MAX_CHILDREN_PER_PARENT:
            return _refuse(
                f"{total} children across all waves; a single fan may launch at most "
                f"{MAX_CHILDREN_PER_PARENT}."
            )

        tree = ctx.tree
        if not tree.can_admit(total):
            return _refuse(
                f"this staged fan of {total} would exceed the tree limit of "
                f"{tree.max_total_agents} sub-agents ({tree.agents_spawned} already running)."
            )
        if not tree.can_admit_budget(total):
            return _refuse(
                f"the remaining sub-agent budget (~{tree.children_remaining()} tokens) is too low "
                f"to launch {total} children; narrow the fan or let the current work finish."
            )

        max_parallel = await self._effective_max_parallel(args.get("max_parallel"))
        owner_ctx = SessionContext(principal_id=ctx.session.principal_id, principal_kind="owner")
        sem = asyncio.Semaphore(max_parallel)

        results_by_label: dict[str, _ChildResult] = {}
        all_results: list[_ChildResult] = []

        def record_skip(wp: _WavePlan, reason: str, w_idx: int) -> None:
            res = _ChildResult(
                wp.label,
                wp.persona,
                f"(skipped — {reason})",
                ok=False,
                session_id="",
                skipped=reason,
                wave=w_idx,
                fed_from=tuple(wp.feed),
            )
            results_by_label[wp.label] = res
            all_results.append(res)
            # Surface the skip live too, so the fan shows a distinct skipped row rather
            # than a silently-missing child (no session is minted for a skip, so the id
            # is synthetic and its row is not tappable).
            sid = f"skip:{wp.label}"
            _emit(
                ctx,
                SubagentSpawnedEvent(
                    child_id=sid,
                    persona=wp.persona,
                    label=wp.label,
                    depth=ctx.depth + 1,
                    wave=w_idx,
                    fed_from=list(wp.feed),
                ),
            )
            _emit(
                ctx,
                SubagentDoneEvent(
                    child_id=sid,
                    ok=False,
                    stop_reason="skipped",
                    summary=res.summary,
                    skip_reason=reason,
                    tree_spent=tree.spent,
                    tree_budget=tree.tree_budget,
                ),
            )

        def emit_view() -> None:
            _emit(ctx, ToolViewEvent(tool_call_id="", view=_synthesis_view(all_results)))

        # Final-wave reserve (F2): carve a viable slice for the LAST wave's consumers off
        # the top before any producer runs, so an over-spending earlier wave cannot
        # starve the deliverable wave. Released when the final wave itself starts. Reset
        # in `finally` so a shared tree never leaks the reserve to a later fan.
        tree.stage_reserve = len(wave_plans[-1]) * MIN_VIABLE_CHILD_BUDGET
        try:
            for w_idx, plans in enumerate(wave_plans):
                if w_idx == len(wave_plans) - 1:
                    tree.stage_reserve = 0  # release the reserve to the final wave

                # Barrier check — wall-clock deadline (F2): a wave that cannot start in
                # time is skipped loud, never silently dropped.
                if tree.out_of_time():
                    for wp in plans:
                        record_skip(wp, "tree wall-clock deadline reached", w_idx)
                    emit_view()
                    continue

                # Fail-closed skip-cascade: a consumer whose fed producer is not a clean
                # success (keyed on `ok`, never on summary text) is skipped, not run.
                runnable: list[_WavePlan] = []
                for wp in plans:
                    missing = [
                        f
                        for f in wp.feed
                        if not (results_by_label.get(f) and results_by_label[f].ok)
                    ]
                    if missing:
                        record_skip(wp, f"upstream {', '.join(missing)} unavailable", w_idx)
                        emit_view()
                        continue
                    runnable.append(wp)
                if not runnable:
                    continue

                # Barrier check — budget re-admission (F2): reflects earlier waves'
                # actual spend; if the pool can no longer seat this wave, skip it loud.
                if not tree.can_admit_budget(len(runnable)):
                    for wp in runnable:
                        record_skip(wp, "sub-agent budget spent by earlier waves", w_idx)
                    emit_view()
                    continue

                # Admit + mint only this wave's runnable children (per-wave: a skipped or
                # never-reached wave never orphans a session or burns a tree slot).
                tree.admit(len(runnable))
                minted: list[tuple[_ChildPlan, AgentSessionInfo]] = []
                for wp in runnable:
                    feed_block = compose_feed_block(
                        [
                            (
                                results_by_label[f].label,
                                results_by_label[f].persona,
                                results_by_label[f].summary,
                            )
                            for f in wp.feed
                        ]
                    )
                    plan = _ChildPlan(
                        wp.persona, wp.label, prepend_feed(feed_block, wp.base_brief), wp.effort
                    )
                    child = await self._sessions.create(
                        owner_ctx,
                        domain_scopes=[],
                        title=wp.label[:_TITLE_LEN],
                        agent=wp.persona,
                        parent_session_id=ctx.agent_session_id,
                        depth=ctx.depth + 1,
                        no_memory=True,
                    )
                    _emit(
                        ctx,
                        SubagentSpawnedEvent(
                            child_id=child.id,
                            persona=wp.persona,
                            label=wp.label,
                            depth=ctx.depth + 1,
                            wave=w_idx,
                            fed_from=list(wp.feed),
                        ),
                    )
                    minted.append((plan, child))

                wave_results = await self._run_wave(
                    ctx, owner_ctx, tree, sem, minted, list(all_results)
                )
                # Stamp wave placement + feed edges onto each result (the runner doesn't
                # know them); order matches `runnable` (gather preserves mint order).
                for res, wp in zip(wave_results, runnable, strict=True):
                    stamped = replace(res, wave=w_idx, fed_from=tuple(wp.feed))
                    results_by_label[stamped.label] = stamped
                    all_results.append(stamped)
        finally:
            tree.stage_reserve = 0

        return ToolOutput(_observation(all_results), view=_synthesis_view(all_results))

    async def _run_wave(
        self,
        ctx: ToolContext,
        owner_ctx: SessionContext,
        tree: TreeState,
        sem: asyncio.Semaphore,
        minted: list[tuple[_ChildPlan, "AgentSessionInfo"]],
        prior_results: list[_ChildResult],
    ) -> list[_ChildResult]:
        """Run one wave's minted children concurrently and await them all — the barrier
        before the next wave. Re-emits the whole-run synthesis view (prior waves +
        this wave's settled-so-far) at each settle, so a cut-short turn still persists a
        view (mirrors the flat path)."""
        collected: list[_ChildResult | None] = [None] * len(minted)

        async def _run_and_collect(
            i: int, plan: _ChildPlan, child: AgentSessionInfo
        ) -> _ChildResult:
            res = await self._run_child(ctx, owner_ctx, tree, sem, plan, child)
            collected[i] = res
            settled = prior_results + [r for r in collected if r is not None]
            _emit(ctx, ToolViewEvent(tool_call_id="", view=_synthesis_view(settled)))
            return res

        return list(
            await asyncio.gather(
                *(_run_and_collect(i, plan, child) for i, (plan, child) in enumerate(minted))
            )
        )

    async def _effective_max_parallel(self, requested: object) -> int:
        """How many children may run at once. The model-requested value is clamped to
        MAX_PARALLEL — but on a LOCAL route it is forced to 1. A single-GPU local model
        serializes every call, so a "parallel" fan just splits the device N ways: each
        child runs at ~1/N throughput and is far likelier to hit its wall-clock. Serial
        gives each child the whole device (so it finishes in time) at ~the same total
        wall-clock, since generation serializes either way."""
        n = requested if isinstance(requested, int) and requested >= 1 else MAX_PARALLEL
        n = min(n, MAX_PARALLEL)
        provider, _model = await self._router.effective_spec(_CHILD_TASK)
        return 1 if provider == "local" else n

    async def _run_child(
        self,
        ctx: ToolContext,
        owner_ctx: SessionContext,
        tree: TreeState,
        sem: asyncio.Semaphore,
        plan: _ChildPlan,
        child: AgentSessionInfo,  # pre-minted; its spawned-event was already emitted
    ) -> _ChildResult:
        persona, label, brief_text = plan.persona, plan.label, plan.brief_text
        # persona is validated ∈ SUBAGENT_PERSONAS, so agent_for never falls back to
        # the KB-capable curator here.
        profile = agent_for(persona)
        # Parent⊆child clamp: the child can hold at most the parent's effective tools,
        # intersected with its persona's allowlist. Passed as the child loop's
        # tools_allow and refused at dispatch.
        child_tools = effective_child_tools(profile.tools, ctx.agent_tools)
        child_depth = ctx.depth + 1
        async with sem:
            # Now actually running (the session was minted + announced up front): flip
            # this child from "queued" to its working phase.
            _emit(
                ctx,
                SubagentProgressEvent(
                    child_id=child.id,
                    phase=_PHASE.get(persona, "working"),
                    tree_spent=tree.spent,
                    tree_budget=tree.tree_budget,
                ),
            )
            child_run = await self._runlog.start(
                owner_ctx,
                session_id=child.id,
                prompt_version=profile.version,
                kind="subagent",
                parent_run_id=ctx.run_id,
            )
            tally = StepTally(self._runlog.bound(owner_ctx, child_run))
            loop = AgentLoop(
                self._router,
                self._registry,
                recorder=tally,  # type: ignore[arg-type]
                # The step cap scales with the child's effort (a high-effort research
                # child gets a long chain to search/read/synthesize); the wall-clock and
                # token caps are generous backstops above it.
                guardrails=Guardrails(
                    max_steps=child_steps_for(plan.effort),
                    max_cost_tokens=CHILD_MAX_COST_TOKENS,
                ),
            )
            child_read_ctx = read_context(owner_ctx.principal_id, ())
            conversation = [
                UserMessage(text=now_block(ctx.timezone)),
                UserMessage(text=brief_text),
            ]

            def _on_step(step: int, _cost: int) -> None:
                # Live per-step progress so the UI's budget meter + step count move while
                # the child works (Wave S2 follow-up).
                _emit(
                    ctx,
                    SubagentProgressEvent(
                        child_id=child.id,
                        phase=_PHASE.get(persona, "working"),
                        step=step,
                        tree_spent=tree.spent,
                        tree_budget=tree.tree_budget,
                    ),
                )

            def _on_text(text: str) -> None:
                # Forward the child's live answer tokens onto the parent stream so the fan
                # row shows it writing in real time (Wave S3 follow-up).
                _emit(ctx, SubagentDeltaEvent(child_id=child.id, channel="answer", text=text))

            def _on_reasoning(text: str) -> None:
                _emit(ctx, SubagentDeltaEvent(child_id=child.id, channel="reasoning", text=text))

            # The child model's context window — the meter's denominator. Resolved once
            # per child (cheap, cached in the router) so its fill bar reads against the
            # same window the child actually runs with.
            child_window = await self._router.context_window("agent.turn")

            def _on_usage(inp: int, out: int) -> None:
                # The child's live context fill, forwarded as the fan row's context meter
                # (the non-streaming twin of the parent turn's usage event).
                _emit(
                    ctx,
                    SubagentUsageEvent(
                        child_id=child.id, used=inp + out, context_window=child_window
                    ),
                )

            def _on_tool(name: str, args: dict, ok: bool) -> None:
                # Forward the child's tool step so the fan frame shows its work as a live
                # "Worked" list (the query / url it used, like the main step rows).
                _emit(
                    ctx,
                    SubagentToolEvent(
                        child_id=child.id, name=name, arg=_tool_arg(name, args), ok=ok
                    ),
                )

            # A flat fan honors the tree wall-clock too, not just the per-child clock:
            # bound each child by whichever deadline is sooner, so a runaway flat fan can't
            # outlive TREE_WALL_CLOCK_S the way an unbounded one used to (a stalled fan
            # hammering blocked sites otherwise ran on to the per-child cap × batches).
            # Computed before the try so it's always bound for the timeout handler below.
            _tree_left = tree.seconds_left()
            child_timeout = CHILD_WALL_CLOCK_S
            if _tree_left is not None:
                child_timeout = min(CHILD_WALL_CLOCK_S, _tree_left)
            try:
                result = await asyncio.wait_for(
                    loop.run(
                        session=child_read_ctx,
                        scopes=(),
                        conversation=conversation,
                        timezone=ctx.timezone,
                        system=profile.prompt,
                        agent_session_id=child.id,
                        tools_allow=child_tools,
                        depth=child_depth,
                        tree=tree,
                        run_id=child_run,
                        on_step=_on_step,
                        # Stream the child's live answer/reasoning/tool steps to the fan.
                        on_text=_on_text,
                        on_reasoning=_on_reasoning,
                        on_tool=_on_tool,
                        on_usage=_on_usage,
                        # The spawner's per-child reasoning effort (the router drops it
                        # for a non-reasoning child model).
                        reasoning_effort=plan.effort,
                        # On step exhaustion, synthesize a final answer from what was
                        # gathered rather than returning an empty "(no answer)".
                        force_final_answer=True,
                    ),
                    timeout=child_timeout,
                )
            except asyncio.CancelledError:
                # A parent cancel cascades into the fan; mark the run best-effort and
                # let the cancellation propagate (it must not be swallowed).
                with contextlib.suppress(Exception):
                    await self._runlog.finish(
                        owner_ctx,
                        child_run,
                        status="error",
                        stop_reason="cancelled",
                        step_count=tally.steps,
                        cost_tokens=tally.cost,
                    )
                raise
            except TimeoutError:
                # The per-child wall-clock fired (wait_for cancelled the run). One slow
                # child must not stall the fan — degrade it and move on.
                secs = int(child_timeout)
                with contextlib.suppress(Exception):
                    await self._runlog.finish(
                        owner_ctx,
                        child_run,
                        status="error",
                        stop_reason="timeout",
                        step_count=tally.steps,
                        cost_tokens=tally.cost,
                    )
                _emit(
                    ctx,
                    SubagentDoneEvent(
                        child_id=child.id,
                        ok=False,
                        stop_reason="timeout",
                        summary=f"(timed out after {secs}s — no answer)",
                        tree_spent=tree.spent,
                        tree_budget=tree.tree_budget,
                    ),
                )
                timeout_summary = f"(timed out after {secs}s — no answer)"
                await self._persist_child(
                    owner_ctx, child.id, child_run, brief_text, timeout_summary
                )
                return _ChildResult(label, persona, timeout_summary, ok=False, session_id=child.id)
            except Exception as exc:  # noqa: BLE001 — a child failure degrades, not crashes
                log.warning("subagent.child_failed", persona=persona, label=label, error=repr(exc))
                with contextlib.suppress(Exception):
                    await self._runlog.finish(
                        owner_ctx,
                        child_run,
                        status="error",
                        stop_reason="error",
                        step_count=tally.steps,
                        cost_tokens=tally.cost,
                    )
                _emit(
                    ctx,
                    SubagentDoneEvent(
                        child_id=child.id,
                        ok=False,
                        stop_reason="error",
                        summary=f"ERROR: {exc}",
                        tree_spent=tree.spent,
                        tree_budget=tree.tree_budget,
                    ),
                )
                await self._persist_child(
                    owner_ctx, child.id, child_run, brief_text, f"ERROR: {exc}"
                )
                return _ChildResult(label, persona, f"ERROR: {exc}", ok=False, session_id=child.id)
            await self._runlog.finish(
                owner_ctx,
                child_run,
                status="done",
                stop_reason=result.stop_reason,
                step_count=tally.steps,
                cost_tokens=tally.cost,
            )
            # A child is a success only if it produced a substantive answer. A clean
            # `end_turn`, or a step/budget-limited stop that still SYNTHESIZED an answer
            # (force_final_answer / a budget-cut partial), counts — it's real, just
            # partial. An empty answer or too_many_errors is degraded, surfaced as
            # [FAILED] so the parent doesn't synthesize over an empty block. (AgentResult
            # never carries stop_reason="error"; an exception-failed child returns above.)
            text = result.text.strip()
            _clean_stops = ("end_turn", "budget", "tree_budget_exhausted", "max_steps")
            hit_cap = result.stop_reason in ("budget", "tree_budget_exhausted", "max_steps")
            ok = bool(text) and result.stop_reason in _clean_stops
            # "Truncated" (the synthesis card's red ✕) is reserved for a child a cap cut
            # off WITHOUT a usable answer. A capped child that still synthesized a real
            # forced-final answer is complete-but-deep, not truncated — so the card stops
            # crying wolf over good research (the common case now the loop soft-lands
            # before the cap). The parent reads the answer as complete.
            truncated = hit_cap and not text
            summary = text if text else f"(no answer; stopped: {result.stop_reason})"
            _emit(
                ctx,
                SubagentDoneEvent(
                    child_id=child.id,
                    ok=ok,
                    stop_reason=result.stop_reason,
                    summary=summary,
                    tree_spent=tree.spent,
                    tree_budget=tree.tree_budget,
                ),
            )
            await self._persist_child(owner_ctx, child.id, child_run, brief_text, summary)
            return _ChildResult(
                label,
                persona,
                summary,
                ok=ok,
                session_id=child.id,
                truncated=truncated,
                # The real URLs the child reached — the favicon citation targets, carried
                # up so the parent can keep them tied to the findings (see field doc).
                web_sources=tuple(result.web_sources),
            )

    async def _persist_child(
        self, owner_ctx: SessionContext, child_id: str, run_id: str, brief: str, answer: str
    ) -> None:
        """Record the child's brief→answer to its own transcript so opening the child
        in the sessions rail replays its work instead of an empty conversation. Gated
        on a configured store (headless/test callers may omit it) and best-effort — a
        write failure never breaks the fan. Tool steps aren't replayed (the
        non-streaming child loop doesn't surface them); the brief and answer are."""
        if self._transcript is None:
            return
        with contextlib.suppress(Exception):
            await self._transcript.record_exchange(
                owner_ctx,
                session_id=child_id,
                run_id=run_id,
                user_text=brief,
                assistant_text=answer,
                tools=[],
            )


def _observation(results: list[_ChildResult]) -> str:
    """Fold the fan's summaries into one observation for the parent to synthesize —
    stable label order, each child framed as data. A failure is surfaced ([FAILED]);
    a fed consumer that never ran is surfaced distinctly ([SKIPPED: …]) so the parent
    reads it as a cascade, not a crash, and never synthesizes over an empty block."""
    ran = sum(1 for r in results if not r.skipped)
    failed = sum(1 for r in results if not r.ok and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    header = (
        f"Sub-agent results — {ran} ran"
        + (f", {failed} failed" if failed else "")
        + (f", {skipped} skipped" if skipped else "")
    )
    blocks = []
    for r in results:
        tag = f" [SKIPPED: {r.skipped}]" if r.skipped else ("" if r.ok else " [FAILED]")
        blocks.append(f"## {r.label} ({r.persona}){tag}\n{r.summary}".rstrip())
    return header + "\n\n" + "\n\n".join(blocks)


def _synthesis_view(results: list[_ChildResult]) -> ViewPayload:
    """The registered `subagent_synthesis` tool-view (DESIGN.md): the fan result as a
    neutral structured card — a per-child roster (label, persona, ok, summary) plus a
    ran/failed roll-up — composed by the PWA from the standard primitives, never a
    bespoke panel. Data-only (the model authors nothing here)."""
    # A skipped consumer never RAN — it must not inflate `ran` or count as `failed`
    # (a cascade/resource skip is distinct from a crash). F1 review fix.
    ran = sum(1 for r in results if not r.skipped)
    failed = sum(1 for r in results if not r.ok and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    return ViewPayload(
        view="subagent_synthesis",
        data={
            "ran": ran,
            "failed": failed,
            "skipped": skipped,
            # Any child cut off on budget makes the whole synthesis partial — the card
            # renders the "research truncated" variant (M7).
            "truncated": any(r.truncated for r in results),
            "children": [
                {
                    "label": r.label,
                    "persona": r.persona,
                    "ok": r.ok,
                    "summary": r.summary,
                    # The child's session id, so the card row can open the sub-agent's
                    # own session (its full transcript) on tap. Empty for a skip.
                    "session_id": r.session_id,
                    # A staged consumer that never ran, and why — rendered distinctly
                    # from a failure by the grouped-by-wave surface (F3).
                    "skipped": bool(r.skipped),
                    "skip_reason": r.skipped,
                    # Staged placement: which wave the row sits in, and the producers
                    # fed into it (the "← fed by …" edge). 0/[] for a flat fan.
                    "wave": r.wave,
                    "fed_from": list(r.fed_from),
                }
                for r in results
            ],
        },
    )


class SpawnRef:
    """Late-bound handler for the `spawn_subagent` tool. The handler must reference
    the SpawnService, which in turn needs the very registry being built (it launches
    children on it) — so the registry is built first, then `service` is set. The
    handler only runs at request time, long after build, so the ref is always bound
    by then; an unbound ref (no router configured) refuses cleanly."""

    def __init__(self) -> None:
        self.service: SpawnService | None = None

    async def __call__(self, args: dict, ctx: ToolContext) -> str:
        if self.service is None:
            return _refuse("sub-agent spawning is not available in this configuration.")
        return await self.service.spawn_fan(ctx, args)
