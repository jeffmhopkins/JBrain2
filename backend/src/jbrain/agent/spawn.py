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
from dataclasses import dataclass

import structlog

from jbrain.agent.agents import SUBAGENT_PERSONAS, agent_for
from jbrain.agent.briefs import BriefError, render_brief
from jbrain.agent.clock import now_block
from jbrain.agent.contracts import (
    ChatEvent,
    SubagentDoneEvent,
    SubagentProgressEvent,
    SubagentSpawnedEvent,
)
from jbrain.agent.loop import AgentLoop, ToolContext, guardrails_for_effort
from jbrain.agent.runlog import AgentRunLog, StepTally
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.tree import MAX_CHILDREN_PER_PARENT, MAX_DEPTH, MAX_PARALLEL, TreeState
from jbrain.db.session import SessionContext
from jbrain.llm import LlmRouter, UserMessage

log = structlog.get_logger(__name__)

_TITLE_LEN = 120  # a child session title is a short label; longer is clamped
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
class _ChildResult:
    label: str
    persona: str
    summary: str
    ok: bool


def effective_child_tools(
    persona_tools: frozenset[str] | None, parent_tools: frozenset[str]
) -> frozenset[str]:
    """The parent⊆child clamp: a child holds at most its persona's allowlist
    intersected with the parent's effective tools — never more than the parent, even
    if the persona lists a tool the parent lacks. Passed as the child loop's
    `tools_allow` and re-enforced at dispatch."""
    return (persona_tools or frozenset()) & parent_tools


def _refuse(reason: str) -> str:
    """A refused spawn is a normal observation (is_error=False so it does not trip
    the consecutive-error guardrail) the model can read and act on."""
    return f"Spawn refused: {reason}"


def _resolve_brief(brief: object, *, depth: int) -> str:
    """The brief a child receives, by the spawner's depth (decision #7). At depth 0
    a free-text string is allowed; at depth >= 1 it MUST be a template-bound
    `{template_id, params}` mapping (fail closed otherwise) so attacker-controlled
    fetched content cannot be laundered into a grandchild's steering instructions."""
    if depth == 0:
        if not isinstance(brief, str):
            raise BriefError("a depth-0 brief must be free text (a string)")
        if not brief.strip():
            raise BriefError("the brief is empty")
        return brief
    # depth >= 1: template-bound only.
    if not isinstance(brief, dict):
        raise BriefError(
            "a depth>=1 brief must be template-bound ({template_id, params}), not free text"
        )
    template_id = brief.get("template_id")
    params = brief.get("params")
    if not isinstance(template_id, str) or not isinstance(params, dict):
        raise BriefError("a template-bound brief needs a string template_id and a params object")
    return render_brief(template_id, params)


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
    ) -> None:
        self._router = router
        self._registry = registry
        self._sessions = sessions
        self._runlog = runlog

    async def spawn_fan(self, ctx: ToolContext, args: dict) -> str:
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

        # --- validate every child up front (persona + brief), reject the fan on
        #     the first bad one so a malformed/injected persona never resolves -----
        plans: list[tuple[str, str, str]] = []  # (persona, label, brief_text)
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
            plans.append((persona, label, brief_text))

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

        max_parallel = args.get("max_parallel")
        if not isinstance(max_parallel, int) or max_parallel < 1:
            max_parallel = MAX_PARALLEL
        max_parallel = min(max_parallel, MAX_PARALLEL)

        owner_ctx = SessionContext(principal_id=ctx.session.principal_id, principal_kind="owner")
        effort = await self._router.effective_reasoning_effort("agent.turn")
        sem = asyncio.Semaphore(max_parallel)

        results = await asyncio.gather(
            *(
                self._run_child(ctx, owner_ctx, tree, sem, effort, persona, label, brief_text)
                for persona, label, brief_text in plans
            )
        )
        return _observation(results)

    async def _run_child(
        self,
        ctx: ToolContext,
        owner_ctx: SessionContext,
        tree: TreeState,
        sem: asyncio.Semaphore,
        effort: str | None,
        persona: str,
        label: str,
        brief_text: str,
    ) -> _ChildResult:
        # persona is validated ∈ SUBAGENT_PERSONAS, so agent_for never falls back to
        # the KB-capable curator here.
        profile = agent_for(persona)
        # Parent⊆child clamp: the child can hold at most the parent's effective tools,
        # intersected with its persona's allowlist. Passed as the child loop's
        # tools_allow and refused at dispatch.
        child_tools = effective_child_tools(profile.tools, ctx.agent_tools)
        child_depth = ctx.depth + 1
        async with sem:
            # Session + run are owner-only; reads run under empty scope (web-sandbox).
            child = await self._sessions.create(
                owner_ctx,
                domain_scopes=[],
                title=label[:_TITLE_LEN],
                agent=persona,
                parent_session_id=ctx.agent_session_id,
                depth=child_depth,
                no_memory=True,
            )
            _emit(
                ctx,
                SubagentSpawnedEvent(
                    child_id=child.id, persona=persona, label=label, depth=child_depth
                ),
            )
            _emit(
                ctx,
                SubagentProgressEvent(child_id=child.id, phase=_PHASE.get(persona, "working")),
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
                guardrails=guardrails_for_effort(effort, scale=profile.budget_multiplier),
            )
            child_read_ctx = read_context(owner_ctx.principal_id, ())
            conversation = [
                UserMessage(text=now_block(ctx.timezone)),
                UserMessage(text=brief_text),
            ]
            try:
                result = await loop.run(
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
                _emit(ctx, SubagentDoneEvent(child_id=child.id, ok=False, stop_reason="error"))
                return _ChildResult(label, persona, f"ERROR: {exc}", ok=False)
            await self._runlog.finish(
                owner_ctx,
                child_run,
                status="done",
                stop_reason=result.stop_reason,
                step_count=tally.steps,
                cost_tokens=tally.cost,
            )
            # A child is a success only if it produced a substantive answer via a
            # clean stop. max_steps / too_many_errors (or an empty answer) is a
            # degraded child — surfaced as [FAILED] so the parent doesn't synthesize
            # over an empty block as if it were a clean summary. (AgentResult never
            # carries stop_reason="error"; an exception-failed child returns above.)
            text = result.text.strip()
            _clean_stops = ("end_turn", "budget", "tree_budget_exhausted")
            truncated = result.stop_reason in ("budget", "tree_budget_exhausted")
            ok = bool(text) and result.stop_reason in _clean_stops
            if not text:
                summary = f"(no answer; stopped: {result.stop_reason})"
            elif truncated:
                # A budget-cut child has a real but PARTIAL answer — tell the parent so
                # it doesn't synthesize over truncated work as if it were complete.
                summary = f"{text}\n\n[truncated — hit the {result.stop_reason} limit]"
            else:
                summary = text
            _emit(ctx, SubagentDoneEvent(child_id=child.id, ok=ok, stop_reason=result.stop_reason))
            return _ChildResult(label, persona, summary, ok=ok)


def _observation(results: list[_ChildResult]) -> str:
    """Fold the fan's summaries into one observation for the parent to synthesize —
    stable label order, each child framed as data (a failure is surfaced, not
    swallowed)."""
    ran = len(results)
    failed = sum(1 for r in results if not r.ok)
    header = f"Sub-agent results — {ran} ran" + (f", {failed} failed" if failed else "")
    blocks = [
        f"## {r.label} ({r.persona}){'' if r.ok else ' [FAILED]'}\n{r.summary}".rstrip()
        for r in results
    ]
    return header + "\n\n" + "\n\n".join(blocks)


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
