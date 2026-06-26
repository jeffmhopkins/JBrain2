"""Session-manager lifecycle: create / capacity / turn status / reset / delete."""

from __future__ import annotations

import pytest

from jcode_ctl.agent import FakeCodingAgent, TurnEvent
from jcode_ctl.sessions import SessionError, SessionManager
from jcode_ctl.workspace import FakeWorkspace


def _mgr(agent=None, workspace=None, **kw) -> SessionManager:
    counter = {"n": 0}

    def _id() -> str:
        counter["n"] += 1
        return f"s{counter['n']}"

    return SessionManager(
        agent or FakeCodingAgent(),
        workspace or FakeWorkspace(),
        "/work",
        new_id=_id,
        **kw,
    )


async def test_create_clones_into_a_per_session_path() -> None:
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("github.com/me/repo", "main")
    assert s.workspace == "/work/s1"
    assert s.work_branch == "jcode/s1"  # derived when not given
    assert ws.cloned == [("github.com/me/repo", "main", "jcode/s1")]


async def test_capacity_is_enforced() -> None:
    mgr = _mgr(max_sessions=1)
    await mgr.create("r")
    with pytest.raises(SessionError, match="capacity"):
        await mgr.create("r2")


async def test_run_turn_streams_and_returns_to_ready() -> None:
    mgr = _mgr()
    s = await mgr.create("r")
    seen = [ev.type async for ev in mgr.run_turn(s.id, "do it")]
    assert seen[-1] == "done"
    assert mgr.get(s.id).status == "ready"


async def test_run_turn_marks_error_on_error_event() -> None:
    agent = FakeCodingAgent([TurnEvent("error", text="boom"), TurnEvent("done")])
    mgr = _mgr(agent=agent)
    s = await mgr.create("r")
    _ = [ev async for ev in mgr.run_turn(s.id, "x")]
    assert mgr.get(s.id).status == "error"


async def test_reset_and_delete() -> None:
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    await mgr.reset(s.id)
    assert ws.reset_paths and str(ws.reset_paths[0]) == "/work/s1"
    mgr.delete(s.id)
    assert ws.removed and str(ws.removed[0]) == "/work/s1"
    with pytest.raises(SessionError, match="unknown"):
        mgr.get(s.id)


async def test_cancel_delegates_to_agent() -> None:
    agent = FakeCodingAgent()
    mgr = _mgr(agent=agent)
    s = await mgr.create("r")
    await mgr.cancel(s.id)
    assert agent.cancelled == [s.id]


async def test_delete_forgets_agent_state() -> None:
    # Deleting a session drops the agent's per-session state so it can't outlive it.
    agent = FakeCodingAgent()
    mgr = _mgr(agent=agent)
    s = await mgr.create("r")
    mgr.delete(s.id)
    assert agent.forgotten == [s.id]


async def test_session_model_reaches_the_agent() -> None:
    # The model chosen at create (the owner's Settings → LLM selection) must be the
    # model every turn of that session runs against.
    agent = FakeCodingAgent()
    mgr = _mgr(agent=agent)
    s = await mgr.create("r", model="qwen3-coder-next")
    assert s.model == "qwen3-coder-next"
    _ = [ev async for ev in mgr.run_turn(s.id, "do it")]
    assert agent.models == ["qwen3-coder-next"]


async def test_model_defaults_to_empty_when_unset() -> None:
    # No selection → empty model; the agent falls back to its configured default.
    agent = FakeCodingAgent()
    mgr = _mgr(agent=agent)
    s = await mgr.create("r")
    assert s.model == ""
    _ = [ev async for ev in mgr.run_turn(s.id, "x")]
    assert agent.models == [""]
