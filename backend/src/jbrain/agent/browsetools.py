"""jerv's `browse` tool: hand one goal to the browse sub-agent, get back quoted data.

`browse` runs only where this turn's `web_fetch` found the site needs a browser
(`browse_gate`, owner decision 2026-10-05). The handler is the Rule-of-Two seam
(docs/plans/BROWSER_AGENT_PLAN.md §2). What crosses into the sub-agent is the goal and the
start URL — nothing from jerv's context, the session, or the owner's data. What crosses
back is `browse.render_for_caller`'s fenced, quarantined text and the URLs of the pages the
host itself loaded, as citation chips.
"""

from __future__ import annotations

from jbrain.agent import browse_gate, browse_trace, url_provenance
from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.browse import BrowseAgent, render_for_caller
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput

MAX_GOAL_CHARS = 600


def build_browse_handlers(
    agent: BrowseAgent, emit: BrainEmit | None = None
) -> dict[str, ToolHandler]:
    """The `browse` handler. Built only when a browser server is configured; otherwise the
    sidecar is dropped from the registry and the tool does not exist on that box."""

    async def browse_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        goal = " ".join(str(arguments.get("goal", "")).split())
        if not goal:
            return ToolOutput("browse needs a goal: what to find out, on which site.")
        if len(goal) > MAX_GOAL_CHARS:
            return ToolOutput(
                f"That goal is too long ({len(goal)} characters). State it in one or two"
                f" sentences (under {MAX_GOAL_CHARS}): the site, the choice to make, the fact"
                " to read back."
            )
        raw_start = arguments.get("start_url")
        start_url = str(raw_start).strip() if raw_start else None
        # The provenance gate first, as web_fetch applies it: a start_url whose site the
        # conversation never produced is refused before the fetch-first gate can echo it.
        if start_url is not None:
            unseen = url_provenance.refusal(start_url, ctx.seen_sites)
            if unseen is not None:
                return ToolOutput(unseen, result_brief=url_provenance.REFUSAL_BRIEF)
        # The fetch-first gate, before anything runs: no browser for a site this turn's
        # web_fetch did not find needing one (browse_gate's docstring has the why).
        refused = browse_gate.refusal(start_url, ctx.browser_needed)
        if refused is not None:
            return ToolOutput(refused, result_brief="refused · fetch first")
        again = browse_gate.repeat_refusal(start_url, ctx.browsed)
        if again is not None:
            return ToolOutput(again, result_brief="refused · already browsed")
        if emit:
            emit("browse", start_url or goal)
        # The conversation's model pick rides along so the sub-agent runs on the model the
        # owner chose — the SAME reason `browse.step` follows agent.turn.
        run = await agent.run(goal, start_url, spec_override=ctx.model_override)
        browse_gate.record_browse(ctx.browsed, run.outcome, start_url)
        sources = tuple(
            WebSource(url=url, title=url, read=url == run.final_url) for url in run.sources
        )
        brief = (
            ("verified" if run.verified else "unverified")
            if run.answered
            else run.outcome.replace("_", " ")
        )
        # The trace is the OWNER's (a step view); jerv reads `render_for_caller` alone.
        domain = browse_gate.registrable_domain(start_url) if start_url else None
        return ToolOutput(
            render_for_caller(run),
            web_sources=sources,
            result_brief=f"{brief} · {len(run.steps)} steps",
            view=browse_trace.build_view(
                run, ctx.browser_needed.get(domain) if domain is not None else None
            ),
        )

    return {"browse": browse_tool}
