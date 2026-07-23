"""The trusted context for a background deepest-research run (docs/plans/
DEEPEST_RESEARCH_TOOL_PLAN.md, R4).

A deepest run has no `/chat` turn to seed its tree, so this module assembles the
orchestrator's `ToolContext` directly — the ONE trusted place a two-tier
(`max_depth=DEEPEST_MAX_DEPTH`) tree is minted. Everything security-load-bearing about a
background run lives in how this context is built:

- **owner-scoped but KB-less** — `read_context` with EMPTY domain scopes, so the
  orchestrator and its sandboxed children read no owner-domain data (health/finance/
  location never enter the run), exactly like the in-request `deep_research` orchestrator
  (jerv, `reads_knowledge_base=False`). No location either (`here` stays None).
- **the only `max_depth>MAX_DEPTH` mint** — via `TreeState.rooted_deepest`; the
  interactive (`api/agent.py`) and scheduled (`tasks/runner.py`) paths use
  `rooted()`/`TreeState()`, which stay at the default, so the extra tier cannot leak.
- **the owner-set ceiling** — the token + wall-clock bound is the hard terminal condition,
  surfaced to the owner before kickoff (R7).

The context this builds is handed to `DeepResearchService.research(ctx, {mode: "deepest",
…})`; because its tree is two-tier, the gather fan runs `research_deep` task agents (R4
activation in `deep_research.py`). Recording, checkpointing, and notification wrap this in
R5–R7; the builder itself is pure and DB-free.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import ModuleType

import structlog

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.deepest_progress import DeepestProgressChannel
from jbrain.agent.loop import ToolContext
from jbrain.agent.session import read_context
from jbrain.agent.tree import TreeState
from jbrain.external import research_run_state as rrs

log = structlog.get_logger()

# Owner-set per-run ceiling defaults (open decision §9.2 — to be grounded on-box). The
# ceiling is the HARD terminal bound: a run stops when it reaches the token budget or the
# wall-clock, whichever comes first. Sized well above an in-request deep_research run
# (~8M tokens / one turn) because a deepest run is minutes-to-hours over many rounds and
# two agent tiers; the owner may override both per run.
DEEPEST_DEFAULT_CEILING_TOKENS = 50_000_000
DEEPEST_DEFAULT_WALL_CLOCK_S = 3 * 60 * 60.0  # 3 hours

# The chat notice for a non-success terminal exit — a lost/failed run and a watchdog cancel
# read differently to the owner, but neither leaves a saved report.
_TERMINAL_LABEL = {
    "failed": "the run failed — nothing was saved",
    "cancelled": "the run hit its time limit and stopped — nothing was saved",
}


def build_deepest_run_context(
    principal_id: str,
    *,
    agent_session_id: str,
    budget_tokens: int = DEEPEST_DEFAULT_CEILING_TOKENS,
    wall_clock_s: float = DEEPEST_DEFAULT_WALL_CLOCK_S,
    timezone: str | None = None,
) -> ToolContext:
    """The `ToolContext` a background deepest run's orchestrator (depth 0) runs under —
    owner identity so it can mint child sessions and cite, KB-less so it (and its children)
    touch no owner-domain data, and a two-tier tree so the `research_deep` fan activates.
    `agent_tools=JERV_TOOLS` is the ceiling children clamp to (a `research_deep` task agent
    needs `decompose_research` + the web tools, all of which jerv holds).

    `run_id` is left None on purpose: `ToolContext.run_id` is an `app.runs` UUID used ONLY
    to stamp a spawned child's `parent_run_id` (consumed in spawn.py's child-run start), and
    a background orchestrator has
    no `/chat` turn run backing it — unlike the lane's own `run_id` ("deepest-<uuid>"), which
    is the run-state/progress key (text), NOT an `app.runs` id. Threading that text key here
    would make every top-level child spawn's `uuid.UUID(parent_run_id)` raise; None makes the
    orchestrator's direct children root subagent runs (valid — the `runs` CHECK admits a null
    parent), and their own runs still parent the deeper tier normally."""
    return ToolContext(
        session=read_context(principal_id, ()),  # owner, KB-less: no domain scope
        scopes=(),
        timezone=timezone,
        agent_session_id=agent_session_id,
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState.rooted_deepest(budget_tokens=budget_tokens, wall_clock_s=wall_clock_s),
        run_id=None,
    )


async def run_deepest(
    *,
    principal_id: str,
    run_id: str,
    session_id: str,
    question: str,
    maker: object,
    service: object,
    progress: DeepestProgressChannel,
    budget_tokens: int = DEEPEST_DEFAULT_CEILING_TOKENS,
    wall_clock_s: float = DEEPEST_DEFAULT_WALL_CLOCK_S,
    timezone: str | None = None,
    run_state: ModuleType = rrs,
) -> str:
    """The background deepest-research run — the coroutine `DeepestRunLane.launch` supervises.
    It composes the earlier waves into one run: open the checkpoint row (R5), build the
    trusted two-tier context (R4), drive `DeepResearchService` in deepest mode with a
    per-round hook that checkpoints the committed round (R5) and posts progress to the chat
    (R6), then mark the run done and announce it — or, on any failure, mark it failed and
    still post a notice. **Fail-closed**: it never raises into the lane (the lane's watchdog
    handles a hang; this handles a crash). Returns the terminal status for the caller/tests.

    `service`, `run_state`, and `progress` are injected so the whole composition is
    unit-testable with fakes — no LLM, no DB, no live tree."""
    ext_ctx = run_state.run_state_context(principal_id)
    ctx = build_deepest_run_context(
        principal_id,
        agent_session_id=session_id,
        budget_tokens=budget_tokens,
        wall_clock_s=wall_clock_s,
        timezone=timezone,
    )
    owner_ctx = ctx.session  # owner-scoped (KB-less); the progress record_answer is owner-RLS
    tree = ctx.tree
    deadline_utc = datetime.now(UTC) + timedelta(seconds=wall_clock_s)

    with contextlib.suppress(Exception):
        await run_state.create_run(
            maker,
            ext_ctx,
            run_id=run_id,
            session_id=session_id,
            question=question,
            ceiling_tokens=budget_tokens,
            wall_clock_deadline=deadline_utc,
        )

    async def on_round(round_no: int, findings: int) -> None:
        # Commit the round's state (so a restart rewinds to here) and post progress — both
        # best-effort so a persistence/notify hiccup never stalls the research itself.
        with contextlib.suppress(Exception):
            await run_state.checkpoint(
                maker,
                ext_ctx,
                run_id=run_id,
                round=round_no,
                spent_tokens=tree.spent if tree else 0,
                agents_spawned=tree.agents_spawned if tree else 0,
                state={"round": round_no, "findings": findings},
            )
        await progress.round(
            owner_ctx,
            session_id=session_id,
            run_id=run_id,
            round_no=round_no,
            findings=findings,
            coverage_label="in progress",
        )

    status = "failed"
    try:
        try:
            await service.research(  # type: ignore[attr-defined]
                ctx,
                {"question": question, "mode": "deepest"},
                on_round=on_round,
                require_persist=True,  # the report's ONLY delivery is the library write
            )
            status = "done"
        except asyncio.CancelledError:
            # The lane's wall-clock watchdog fired (or an explicit cancel). Record a terminal
            # status and post a notice below, then re-raise so the lane settles the task as
            # cancelled — WITHOUT this, `except Exception` misses CancelledError and the
            # run-state row is left 'running' forever with no notice (a silent strand).
            status = "cancelled"
            raise
        except Exception:  # noqa: BLE001 — a run failure is a recorded status, never a lane crash
            log.warning("deepest_run.failed", run_id=run_id, exc_info=True)
    finally:
        # Every exit — done, failed, or cancelled — records a terminal status and posts a
        # notice, best-effort. A finally's awaits complete even while a CancelledError is
        # propagating, so a watchdog-cancelled run is never left 'running' and always tells
        # the owner it stopped.
        with contextlib.suppress(Exception):
            await run_state.finish(maker, ext_ctx, run_id=run_id, status=status)
        with contextlib.suppress(Exception):
            if status == "done":
                await progress.done(
                    owner_ctx, session_id=session_id, run_id=run_id, question=question
                )
            else:
                await progress.round(
                    owner_ctx,
                    session_id=session_id,
                    run_id=run_id,
                    round_no=0,
                    findings=0,
                    coverage_label=_TERMINAL_LABEL[status],
                )
    return status


def _seconds_left(deadline_utc: datetime | None) -> float:
    """Remaining wall-clock from a checkpoint's ABSOLUTE deadline — restart-safe (a monotonic
    clock would be meaningless across the restart). None (unbounded) falls back to the default."""
    if deadline_utc is None:
        return float(DEEPEST_DEFAULT_WALL_CLOCK_S)
    return max(0.0, (deadline_utc - datetime.now(UTC)).total_seconds())


async def resume_deepest(
    *,
    principal_id: str,
    run_id: str,
    maker: object,
    service: object,
    progress: DeepestProgressChannel,
    run_state: ModuleType = rrs,
) -> str | None:
    """Resume a background run interrupted by a restart. Atomically **claims** the run
    (exactly-once — a second process, or a retry, gets None), rehydrates its question /
    session / ceiling from the checkpoint, and re-drives it — producing a coverage-equivalent
    report over the accumulated library (the report persist dedups on `(question_hash,
    tool)`, so the finished report replaces the run's own earlier partial, never a deep one).
    Returns the terminal status, or None if the run couldn't be claimed (already claimed, not
    running, or gone). The tree's committed counters are available via `TreeState.for_resume`
    for a future *continue-from-round*; this entry re-runs the pipeline, which the gate's
    coverage-equivalent bar (not byte-equality — the LLM calls are non-seeded) permits."""
    ext_ctx = run_state.run_state_context(principal_id)
    if not await run_state.claim_resume(maker, ext_ctx, run_id):
        return None  # not ours to resume (already claimed / finished)
    st = await run_state.load(maker, ext_ctx, run_id)
    if st is None or st.status != "running":
        return None
    return await run_deepest(
        principal_id=principal_id,
        run_id=run_id,
        session_id=st.session_id or "",
        question=st.question,
        maker=maker,
        service=service,
        progress=progress,
        budget_tokens=st.ceiling_tokens,
        wall_clock_s=_seconds_left(st.wall_clock_deadline),
        run_state=run_state,
    )
