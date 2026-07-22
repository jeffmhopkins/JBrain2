"""The trusted deepest-run context builder (DEEPEST_RESEARCH_TOOL_PLAN.md, R4): the one
place a background deepest run's context is assembled. Pure, DB-free — the assertions are
the security properties of that context: owner-scoped but KB-less, the only max_depth>1
mint, no location, and the clamp ceiling a research_deep task agent needs."""

from jbrain.agent.deepest_run import (
    DEEPEST_DEFAULT_CEILING_TOKENS,
    DEEPEST_DEFAULT_WALL_CLOCK_S,
    build_deepest_run_context,
)
from jbrain.agent.tree import DEEPEST_MAX_DEPTH, MAX_DEPTH


def test_context_is_owner_scoped_but_kb_less() -> None:
    """Owner identity (so it can mint child sessions and cite) but EMPTY domain scopes —
    the orchestrator and its children read no owner-domain data (the health/finance/
    location firewalls never enter the run), exactly like the in-request jerv orchestrator."""
    ctx = build_deepest_run_context("owner-1", agent_session_id="s1", run_id="r1")
    assert ctx.session.principal_id == "owner-1"
    assert ctx.session.principal_kind == "owner"
    assert ctx.session.domain_scopes == ()  # KB-less: cannot read ANY domain, not just cross-domain
    assert ctx.scopes == ()


def test_context_is_the_only_two_tier_mint_and_has_no_location() -> None:
    ctx = build_deepest_run_context("owner-1", agent_session_id="s1", run_id="r1")
    assert ctx.depth == 0
    assert ctx.tree is not None
    assert ctx.tree.max_depth == DEEPEST_MAX_DEPTH > MAX_DEPTH  # the two-tier seed
    assert ctx.here is None and ctx.here_as_of is None  # no location in a background run


def test_context_clamp_ceiling_covers_a_task_agent() -> None:
    """The orchestrator holds what a research_deep task agent must inherit through the
    parent⊆child clamp — decompose_research plus the web tools — else the clamp would
    strip decompose and the second tier could never spawn."""
    ctx = build_deepest_run_context("owner-1", agent_session_id="s1", run_id="r1")
    assert "decompose_research" in ctx.agent_tools
    assert {"web_search", "web_fetch"} <= ctx.agent_tools


def test_ceiling_defaults_apply_and_override() -> None:
    default = build_deepest_run_context("o", agent_session_id="s", run_id="r")
    assert default.tree is not None and default.tree.tree_budget == DEEPEST_DEFAULT_CEILING_TOKENS
    assert DEEPEST_DEFAULT_WALL_CLOCK_S > 0
    custom = build_deepest_run_context(
        "o", agent_session_id="s", run_id="r", budget_tokens=1_000_000, wall_clock_s=60
    )
    assert custom.tree is not None and custom.tree.tree_budget == 1_000_000
