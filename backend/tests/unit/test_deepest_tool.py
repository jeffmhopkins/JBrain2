"""The deepest_research kickoff tool (DEEPEST_RESEARCH_TOOL_PLAN.md, R7): enqueue-and-return.
Proven with a fake lane (which records the launch but never runs the coroutine), so the
assertions are the kickoff contract — the guards, the non-blocking launch, and the
already-in-flight path — without an LLM, DB, or a real background run."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from jbrain.agent import deepest_tool as dt
from jbrain.agent.deepest_tool import DeepestKickoffService
from jbrain.agent.loop import ToolContext
from jbrain.agent.tree import MAX_DEPTH, TreeState
from jbrain.db.session import SessionContext


class _FakeLane:
    def __init__(self, *, accept: bool = True) -> None:
        self.launches: list[tuple[str, float]] = []
        self._accept = accept
        self.drained = False

    def launch(self, run_id: str, run: Any, *, wall_clock_s: float) -> bool:
        self.launches.append((run_id, wall_clock_s))
        return self._accept  # never actually runs `run` — the contract is the launch

    async def drain(self) -> None:
        self.drained = True


class _FakeRunState:
    """Stands in for research_run_state for the resume sweep — only `run_state_context` +
    `list_running` are exercised (the lane never runs the resume coroutine)."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.listed = False

    def run_state_context(self, principal_id: str) -> SessionContext:
        return SessionContext(
            principal_id=principal_id, principal_kind="owner", domain_scopes=("external",)
        )

    async def list_running(self, maker: Any, ctx: Any) -> list[Any]:
        self.listed = True
        return self.rows


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


# --- resume + drain (R5): the app-lifespan hooks that survive a restart -----------------


def _svc_with(lane: _FakeLane, run_state: Any) -> DeepestKickoffService:
    return DeepestKickoffService(
        lane=lane,  # type: ignore[arg-type]
        service=object(),
        progress=object(),  # type: ignore[arg-type]
        maker=object(),
        run_state=run_state,
    )


async def test_resume_interrupted_relaunches_every_running_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a restart the sweep resolves the owner, lists running/unclaimed runs, and
    relaunches each on the lane (the atomic claim inside resume_deepest is the exactly-once
    guard; here the fake lane records the launch without running it)."""
    monkeypatch.setattr(dt, "_owner_principal_id", _fake_owner("owner-1"))
    rows = [
        SimpleNamespace(run_id="deepest-a", wall_clock_deadline=None),
        SimpleNamespace(run_id="deepest-b", wall_clock_deadline=None),
    ]
    rs, lane = _FakeRunState(rows), _FakeLane()
    launched = await _svc_with(lane, rs).resume_interrupted()
    assert launched == 2 and rs.listed
    assert [run_id for run_id, _ in lane.launches] == ["deepest-a", "deepest-b"]
    assert all(wall > 0 for _, wall in lane.launches)


async def test_resume_interrupted_noops_without_an_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dt, "_owner_principal_id", _fake_owner(None))
    rs, lane = _FakeRunState([SimpleNamespace(run_id="x", wall_clock_deadline=None)]), _FakeLane()
    assert await _svc_with(lane, rs).resume_interrupted() == 0
    assert not lane.launches and not rs.listed  # bailed before even listing


async def test_resume_interrupted_skips_a_run_past_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose absolute wall-clock deadline already passed has no time left to resume —
    it's a terminal reconcile, not a relaunch, so the sweep skips it (never a 0-second run)."""
    monkeypatch.setattr(dt, "_owner_principal_id", _fake_owner("owner-1"))
    past = datetime.now(UTC) - timedelta(hours=1)
    rs = _FakeRunState([SimpleNamespace(run_id="deepest-old", wall_clock_deadline=past)])
    lane = _FakeLane()
    assert await _svc_with(lane, rs).resume_interrupted() == 0
    assert not lane.launches


async def test_drain_delegates_to_the_lane() -> None:
    lane = _FakeLane()
    await _svc(lane).drain()
    assert lane.drained


def _fake_owner(pid: str | None):  # noqa: ANN202
    async def _owner(_maker: Any) -> str | None:
        return pid

    return _owner
