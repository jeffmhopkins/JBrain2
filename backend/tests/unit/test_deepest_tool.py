"""The deepest_research kickoff tool (DEEPEST_RESEARCH_TOOL_PLAN.md, R7): enqueue-and-return.
Proven with a fake lane (which records the launch but never runs the coroutine), so the
assertions are the kickoff contract — the guards, the non-blocking launch, and the
already-in-flight path — without an LLM, DB, or a real background run."""

from typing import Any

from jbrain.agent.deepest_tool import DeepestKickoffService
from jbrain.agent.loop import ToolContext
from jbrain.agent.tree import MAX_DEPTH, TreeState
from jbrain.db.session import SessionContext


class _FakeLane:
    def __init__(self, *, accept: bool = True) -> None:
        self.launches: list[tuple[str, float]] = []
        self._accept = accept

    def launch(self, run_id: str, run: Any, *, wall_clock_s: float) -> bool:
        self.launches.append((run_id, wall_clock_s))
        return self._accept  # never actually runs `run` — the contract is the launch


def _svc(lane: _FakeLane) -> DeepestKickoffService:
    return DeepestKickoffService(
        lane=lane,  # type: ignore[arg-type]
        service=object(),
        progress=object(),  # type: ignore[arg-type]
        maker=object(),
    )


def _ctx(*, depth: int = 0, seeded: bool = True, session_id: str | None = "chat-1") -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id=session_id,
        depth=depth,
        agent_tools=frozenset({"deepest_research"}),
        tree=TreeState.rooted(800_000) if seeded else None,
        run_id="parent-run",
    )


async def test_kickoff_launches_and_returns_immediately() -> None:
    lane = _FakeLane()
    out = await _svc(lane).kickoff(_ctx(), {"question": "how does X actually work"})
    assert "started" in out.lower() and "deepest-" in out  # the run id is surfaced
    assert len(lane.launches) == 1  # enqueued exactly one background run
    run_id, wall = lane.launches[0]
    assert run_id.startswith("deepest-") and wall > 0


async def test_kickoff_reports_a_run_already_in_flight() -> None:
    lane = _FakeLane(accept=False)  # the lane is at capacity
    out = await _svc(lane).kickoff(_ctx(), {"question": "q"})
    assert "already in progress" in out.lower()


async def test_kickoff_refused_for_a_child_turn() -> None:
    lane = _FakeLane()
    out = await _svc(lane).kickoff(_ctx(depth=MAX_DEPTH), {"question": "q"})
    assert "refused" in out.lower()
    assert not lane.launches


async def test_kickoff_refused_without_a_seeded_tree() -> None:
    lane = _FakeLane()
    out = await _svc(lane).kickoff(_ctx(seeded=False), {"question": "q"})
    assert "refused" in out.lower()
    assert not lane.launches


async def test_kickoff_refused_for_an_empty_question() -> None:
    lane = _FakeLane()
    out = await _svc(lane).kickoff(_ctx(), {"question": "   "})
    assert "refused" in out.lower()
    assert not lane.launches


async def test_kickoff_refused_without_a_chat_session() -> None:
    lane = _FakeLane()
    out = await _svc(lane).kickoff(_ctx(session_id=None), {"question": "q"})
    assert "refused" in out.lower()
    assert not lane.launches
