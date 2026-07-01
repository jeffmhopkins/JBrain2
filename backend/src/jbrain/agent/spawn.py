"""The sub-agent spawn service (docs/SUBAGENT_SPAWNING_PLAN.md, Wave S1).

`jerv` (and, for nesting, a research/review child) calls the `spawn_subagent` tool;
its handler is a `SpawnRef` that forwards to `SpawnService.spawn_fan`. The service
launches a **fan** of web-sandboxed children as in-request `asyncio.gather` tasks
the parent turn awaits (fan-in model A), collects their summaries in stable label
order, and returns them as a single observation the parent then synthesizes.

Every safety property here is structural — enforced with no model cooperation:

- **Persona validation** against the closed `SUBAGENT_PERSONAS` set BEFORE
  `agent_for` (which falls back to the KB-capable curator on an unknown name).
- **Parent⊆child clamp:** a child's effective tools = `persona.tools ∩
  parent.agent_tools`, passed as the child loop's `tools_allow` and refused at
  dispatch. Child read scope is empty (jerv-only-root → no domain data, ever).
- **Depth cap:** spawn refused unless `parent.depth < MAX_DEPTH`.
- **Fan/tree caps:** per-fan size, the tree-wide total, and concurrency.
- **Sandbox:** each child session is `no_memory` (and the helper never records an
  episode), and its `ToolContext.here`/`here_as_of` are None (no location).
- **Brief boundary:** free-text only at depth 0; depth>=1 is template-bound
  (closes the re-spawn laundering hop, decision #7).

A refused or failed spawn is a structured observation (never an exception) so the
model self-corrects; a child that errors degrades to an error summary and the rest
of the fan proceeds. A cancelled parent turn cascades `CancelledError` into the
gathered children.
"""

import asyncio
import contextlib
import re
from dataclasses import dataclass

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
    an event sink (the streaming root turn does; a non-streaming child's own fan does
    not, so a grandchild's events are not surfaced live in v1 — fan-in model A)."""
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
    # anti-orphan requirement, docs/SUBAGENT_FEEDING_WAVES_PLAN.md).
    session_id: str
    truncated: bool = False
    # A consumer that never ran because its fed producer was unavailable: the reason
    # ("upstream <label> failed"). Empty for a child that actually ran. A skipped child
    # is never `ok` and is surfaced distinctly from a failure (it is a cascade, not a
    # crash) — so the parent never synthesizes over an empty block.
    skipped: str = ""


def effective_child_tools(
    persona_tools: frozenset[str] | None, parent_tools: frozenset[str]
) -> frozenset[str]:
    """The parent⊆child clamp: a child holds at most its persona's allowlist
    intersected with the parent's effective tools — never more than the parent, even
    if the persona lists a tool the parent lacks. Passed as the child loop's
    `tools_allow` and re-enforced at dispatch."""
    return (persona_tools or frozenset()) & parent_tools


_TOOL_ARG_KEY = {"web_search": "query", "web_fetch": "url"}
_TOOL_ARG_LEN = 200  # a child tool step's inline preview is short; longer is clamped


def _tool_arg(name: str, args: object) -> str:
    """A short inline preview of a child tool call for the fan's Worked list — the
    searched query or fetched url, like the main step rows. Empty for other tools."""
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
    """A template-bound `{template_id, params}` brief — the depth>=1 / fed form, whose
    slots frame every value as data so untrusted content cannot become instruction.
    Fail closed on any other shape."""
    if not isinstance(brief, dict):
        raise BriefError("this brief must be template-bound ({template_id, params}), not free text")
    template_id = brief.get("template_id")
    params = brief.get("params")
    if not isinstance(template_id, str) or not isinstance(params, dict):
        raise BriefError("a template-bound brief needs template_id (str) and params (object)")
    return render_brief(template_id, params)


def _resolve_brief(brief: object, *, depth: int) -> str:
    """The brief a child receives, by the spawner's depth (decision #7). At depth 0 a
    free-text string is allowed; at depth >= 1 it MUST be template-bound (fail closed)
    so attacker-controlled fetched content cannot be laundered into a grandchild's
    steering instructions."""
    return _free_text_brief(brief) if depth == 0 else _template_brief(brief)


# --- Feeding waves: plan validation (docs/SUBAGENT_FEEDING_WAVES_PLAN.md) ----


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


def _references_unfed_sibling(
    brief: str, self_label: str, all_labels: frozenset[str], feed: frozenset[str]
) -> str | None:
    """Return the offending phrase/label if an un-fed brief refers to data it never
    received — either a guard phrase or another task's label it does not `feed`. This
    turns the silent empty-run into a clean refusal (the primary structural fix)."""
    low = brief.lower()
    for phrase in _GUARD_PHRASES:
        if phrase in low:
            return phrase
    for label in all_labels:
        if label == self_label or label in feed:
            continue
        if re.search(rf"\b{re.escape(label)}\b", brief, re.IGNORECASE):
            return label
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
            try:
                base_brief = _resolve_wave_brief(task.get("brief"), fed=bool(feed))
            except BriefError as exc:
                return [], f"{label}: {exc}"
            if not feed:
                hit = _references_unfed_sibling(base_brief, label, all_labels, frozenset(feed))
                if hit is not None:
                    return [], (
                        f"{label}: the brief refers to {hit!r} but declares no `feed` — add a "
                        "`feed` edge to that producer (or inline the data), so the child "
                        "actually receives it instead of running empty."
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
        is purely additive (docs/SUBAGENT_FEEDING_WAVES_PLAN.md)."""
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
        if ctx.depth >= MAX_DEPTH:
            return _refuse(
                f"already at depth {ctx.depth}; a sub-agent may nest at most {MAX_DEPTH} layers."
            )

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
                brief_text = _resolve_brief(task.get("brief"), depth=ctx.depth)
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
            settled = [r for r in collected if r is not None]
            _emit(ctx, ToolViewEvent(tool_call_id="", view=_synthesis_view(settled)))
            return res

        # asyncio.gather, when this task is cancelled (a Stop / shutdown), cancels every
        # child AND awaits its cancellation cleanup before propagating — the
        # `_GatheringFuture` resolves only once all children are done — so each child's
        # run-log close (status=cancelled) lands inline, not stranded "running". Paired
        # with the loop cancelling this dispatched fan (loop.py), a Stop tears the whole
        # tree down cleanly (test_cancelled_child_runlog_settles_inline_not_detached).
        results = await asyncio.gather(
            *(_run_and_collect(i, plan, child) for i, (plan, child) in enumerate(minted))
        )
        # The text observation is what the parent synthesizes from; the view is the
        # UI's structured render of the same fan result (the registered
        # `subagent_synthesis` tool-view, DESIGN.md). Both carry the same data.
        return ToolOutput(_observation(results), view=_synthesis_view(results))

    async def _spawn_waves(self, ctx: ToolContext, args: dict) -> str:
        """Run an ordered sequence of disconnected waves, feeding each wave's summaries
        forward into the next wave's briefs (docs/SUBAGENT_FEEDING_WAVES_PLAN.md). Each
        wave is a flat fan; a hard barrier (the awaited gather) separates them. Children
        are minted/admitted per wave, so a skipped or never-reached wave orphans nothing.
        A consumer whose fed producer failed is skipped, never run over empty data."""
        if ctx.tree is None:
            return _refuse("sub-agent spawning is only available in an interactive owner turn.")
        # No nesting (decision D4): staged waves are a top-level capability; a nested
        # child spawns a flat fan. Keeps the feed hop single and the surface bounded.
        if ctx.depth != 0:
            return _refuse(
                "staged `waves` are a top-level capability; "
                "a nested sub-agent spawns a flat fan."
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

        for w_idx, plans in enumerate(wave_plans):
            # Fail-closed skip-cascade: a consumer whose fed producer is not a clean
            # success (keyed on `ok`, never on summary text) is skipped, not run.
            runnable: list[_WavePlan] = []
            for wp in plans:
                missing = [
                    f for f in wp.feed if not (results_by_label.get(f) and results_by_label[f].ok)
                ]
                if missing:
                    reason = f"upstream {', '.join(missing)} unavailable"
                    res = _ChildResult(
                        wp.label, wp.persona, f"(skipped — {reason})", ok=False,
                        session_id="", skipped=reason,
                    )
                    results_by_label[wp.label] = res
                    all_results.append(res)
                    _emit(ctx, ToolViewEvent(tool_call_id="", view=_synthesis_view(all_results)))
                    continue
                runnable.append(wp)
            if not runnable:
                continue

            # Admit + mint only this wave's runnable children (per-wave: a skipped or
            # never-reached wave never orphans a session or burns a tree slot).
            tree.admit(len(runnable))
            minted: list[tuple[_ChildPlan, AgentSessionInfo]] = []
            for wp in runnable:
                feed_block = compose_feed_block(
                    [
                        (results_by_label[f].label, results_by_label[f].persona,
                         results_by_label[f].summary)
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
                        child_id=child.id, persona=wp.persona, label=wp.label,
                        depth=ctx.depth + 1, wave=w_idx,
                    ),
                )
                minted.append((plan, child))

            wave_results = await self._run_wave(
                ctx, owner_ctx, tree, sem, minted, list(all_results)
            )
            for res in wave_results:
                results_by_label[res.label] = res
                all_results.append(res)

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
                    timeout=CHILD_WALL_CLOCK_S,
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
                secs = int(CHILD_WALL_CLOCK_S)
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
                label, persona, summary, ok=ok, session_id=child.id, truncated=truncated
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
    ran = len(results)
    failed = sum(1 for r in results if not r.ok)
    return ViewPayload(
        view="subagent_synthesis",
        data={
            "ran": ran,
            "failed": failed,
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
                    # own session (its full transcript) on tap.
                    "session_id": r.session_id,
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
