"""jerv's `browse` tool: hand one goal to the browse sub-agent, get back quoted data.

The handler is the Rule-of-Two seam (docs/plans/BROWSER_AGENT_PLAN.md §2). What crosses
into the sub-agent is the goal and an optional start URL — nothing from jerv's context, the
session, or the owner's data. What crosses back is `browse.render_for_caller`'s fenced,
quarantined text and the URLs of the pages the host itself loaded, as citation chips.
"""

from __future__ import annotations

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
        if emit:
            emit("web_fetch", start_url or goal)
        # The conversation's model pick rides along so the sub-agent runs on the model the
        # owner chose — the SAME reason `browse.step` follows agent.turn.
        run = await agent.run(goal, start_url, spec_override=ctx.model_override)
        sources = tuple(
            WebSource(url=url, title=url, read=url == run.final_url) for url in run.sources
        )
        brief = (
            ("verified" if run.verified else "unverified")
            if run.answered
            else run.outcome.replace("_", " ")
        )
        return ToolOutput(
            render_for_caller(run),
            web_sources=sources,
            result_brief=f"{brief} · {len(run.steps)} steps",
        )

    return {"browse": browse_tool}
