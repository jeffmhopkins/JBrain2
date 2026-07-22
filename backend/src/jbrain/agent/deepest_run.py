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

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.loop import ToolContext
from jbrain.agent.session import read_context
from jbrain.agent.tree import TreeState

# Owner-set per-run ceiling defaults (open decision §9.2 — to be grounded on-box). The
# ceiling is the HARD terminal bound: a run stops when it reaches the token budget or the
# wall-clock, whichever comes first. Sized well above an in-request deep_research run
# (~8M tokens / one turn) because a deepest run is minutes-to-hours over many rounds and
# two agent tiers; the owner may override both per run.
DEEPEST_DEFAULT_CEILING_TOKENS = 50_000_000
DEEPEST_DEFAULT_WALL_CLOCK_S = 3 * 60 * 60.0  # 3 hours


def build_deepest_run_context(
    principal_id: str,
    *,
    agent_session_id: str,
    run_id: str,
    budget_tokens: int = DEEPEST_DEFAULT_CEILING_TOKENS,
    wall_clock_s: float = DEEPEST_DEFAULT_WALL_CLOCK_S,
    timezone: str | None = None,
) -> ToolContext:
    """The `ToolContext` a background deepest run's orchestrator (depth 0) runs under —
    owner identity so it can mint child sessions and cite, KB-less so it (and its children)
    touch no owner-domain data, and a two-tier tree so the `research_deep` fan activates.
    `agent_tools=JERV_TOOLS` is the ceiling children clamp to (a `research_deep` task agent
    needs `decompose_research` + the web tools, all of which jerv holds)."""
    return ToolContext(
        session=read_context(principal_id, ()),  # owner, KB-less: no domain scope
        scopes=(),
        timezone=timezone,
        agent_session_id=agent_session_id,
        depth=0,
        agent_tools=JERV_TOOLS,
        tree=TreeState.rooted_deepest(budget_tokens=budget_tokens, wall_clock_s=wall_clock_s),
        run_id=run_id,
    )
