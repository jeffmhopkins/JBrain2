"""The `deepest_research` kickoff tool (docs/plans/DEEPEST_RESEARCH_TOOL_PLAN.md, R7).

Unlike `deep_research` (which runs inline and returns the report), `deepest_research`
**enqueues and returns**: it mints a run id, launches the background run on the
`DeepestRunLane` (which drives the run detached and concurrent), and hands the owner's turn
straight back with a "run started" acknowledgement. Progress and the finished report arrive
asynchronously in this same chat session (R6). A run already in flight is reported, not
queued (the lane's single-slot default). jerv-only, owner-turn-only, depth-0-only — a
sub-agent can't kick a deepest run, and neither can a non-interactive turn (no seeded tree).
"""

from __future__ import annotations

import uuid

import structlog

from jbrain.agent.deepest_lane import DeepestRunLane
from jbrain.agent.deepest_progress import DeepestProgressChannel
from jbrain.agent.deepest_run import (
    DEEPEST_DEFAULT_CEILING_TOKENS,
    DEEPEST_DEFAULT_WALL_CLOCK_S,
    run_deepest,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.tree import MAX_DEPTH

log = structlog.get_logger()


def _refuse(reason: str) -> str:
    return f"Refused: {reason}"


class DeepestKickoffService:
    """Kicks off a background deepest run and returns immediately. Holds the shared lane and
    the deps a run needs (the `DeepResearchService` it drives, the progress channel, the DB
    maker); a run's coroutine is built per kickoff and launched on the lane."""

    def __init__(
        self,
        *,
        lane: DeepestRunLane,
        service: object,
        progress: DeepestProgressChannel,
        maker: object,
    ) -> None:
        self._lane = lane
        self._service = service
        self._progress = progress
        self._maker = maker

    async def kickoff(self, ctx: ToolContext, args: dict) -> str:
        # Same guards as deep_research: an interactive owner turn (a seeded tree), at depth 0.
        if ctx.tree is None:
            return _refuse("deepest research is only available in an interactive owner turn.")
        if ctx.depth >= MAX_DEPTH:
            return _refuse("a sub-agent cannot start a deepest-research run; only jerv does.")
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            return _refuse("provide a non-empty `question` to research.")
        question = question.strip()
        # Progress + the report land in the chat this was kicked from.
        session_id = ctx.agent_session_id
        if not session_id:
            return _refuse("deepest research needs a chat session to report back into.")
        principal_id = ctx.session.principal_id
        budget_tokens = _clamp_int(args.get("budget_tokens"), DEEPEST_DEFAULT_CEILING_TOKENS)
        wall_clock_s = float(DEEPEST_DEFAULT_WALL_CLOCK_S)
        run_id = f"deepest-{uuid.uuid4()}"

        timezone = ctx.timezone

        async def run() -> None:
            await run_deepest(
                principal_id=principal_id,
                run_id=run_id,
                session_id=session_id,
                question=question,
                maker=self._maker,
                service=self._service,
                progress=self._progress,
                budget_tokens=budget_tokens,
                wall_clock_s=wall_clock_s,
                timezone=timezone,
            )

        if not self._lane.launch(run_id, run, wall_clock_s=wall_clock_s):
            return (
                "A deepest-research run is already in progress. Wait for it to finish (I'll "
                "post the report here) before starting another."
            )
        log.info("deepest_research.kicked_off", run_id=run_id)
        return (
            f"Started a deepest-research run on your question. This runs in the background for "
            f"a while — I'll post progress here and let you know when the report is ready "
            f"(run {run_id})."
        )


def _clamp_int(raw: object, default: int) -> int:
    """An owner-supplied ceiling override, clamped to a sane positive range (never above the
    default hard ceiling — a run may only ask for LESS, never more)."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return default
    return min(raw, default)


class DeepestResearchRef:
    """Late-bound handler for the `deepest_research` tool, mirroring `DeepResearchRef`: the
    kickoff service needs the lane + the (registry-backed) `DeepResearchService`, so it is
    wired once those exist. An unbound ref refuses cleanly."""

    def __init__(self) -> None:
        self.service: DeepestKickoffService | None = None

    async def __call__(self, args: dict, ctx: ToolContext) -> str:
        if self.service is None:
            return _refuse("deepest research is not available in this configuration.")
        return await self.service.kickoff(ctx, args)
