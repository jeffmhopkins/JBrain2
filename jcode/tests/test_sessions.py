"""Session-manager lifecycle: create / capacity / turn status / reset / delete."""

from __future__ import annotations

from pathlib import Path

import pytest

from jcode_ctl.agent import FakeCodingAgent, TurnEvent
from jcode_ctl.sessions import SessionError, SessionManager, directory_size_mb
from jcode_ctl.workspace import FakeWorkspace

_MB = 1024 * 1024


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


async def test_turn_logs_start_and_end(caplog) -> None:
    # The turn lifecycle is logged at INFO so a pulled /debug/logs/jcode is useful.
    import logging

    mgr = _mgr()
    s = await mgr.create("r", model="m1")
    with caplog.at_level(logging.INFO, logger="jcode_ctl.sessions"):
        _ = [ev async for ev in mgr.run_turn(s.id, "do it")]
    msgs = " ".join(r.message for r in caplog.records)
    assert "turn start" in msgs and "turn end" in msgs


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


# --- Wave J5: per-session ceilings -------------------------------------------------


async def test_concurrent_turn_capacity_is_enforced_then_freed() -> None:
    # Two turns in flight saturate a cap of 2; a third is refused. Draining/closing a
    # turn frees its slot so the next one runs — the counter is paired, not leaked.
    mgr = _mgr(max_concurrent_turns=2)
    s1 = await mgr.create("r")
    s2 = await mgr.create("r")
    s3 = await mgr.create("r")
    g1 = mgr.run_turn(s1.id, "x")
    g2 = mgr.run_turn(s2.id, "x")
    await g1.__anext__()  # first event: this turn is now active (1)
    await g2.__anext__()  # active (2) — at the cap
    with pytest.raises(SessionError, match="turn capacity"):
        await mgr.run_turn(s3.id, "x").__anext__()
    # Drain both in-flight turns → both slots freed (the counter is paired in finally).
    _ = [ev async for ev in g1]
    _ = [ev async for ev in g2]
    seen = [ev.type async for ev in mgr.run_turn(s3.id, "x")]
    assert seen[-1] == "done"


async def test_a_refused_turn_leaves_the_session_untouched() -> None:
    # The capacity guard fires BEFORE the session is marked running / the counter moves,
    # so a rejected turn doesn't strand the session in `running` or leak a slot.
    mgr = _mgr(max_concurrent_turns=1)
    s1 = await mgr.create("r")
    s2 = await mgr.create("r")
    g1 = mgr.run_turn(s1.id, "x")
    await g1.__anext__()
    with pytest.raises(SessionError, match="turn capacity"):
        await mgr.run_turn(s2.id, "x").__anext__()
    assert mgr.get(s2.id).status == "ready"
    _ = [ev async for ev in g1]  # free the slot; s2 can run now
    seen = [ev.type async for ev in mgr.run_turn(s2.id, "x")]
    assert seen[-1] == "done"


async def test_disk_quota_refuses_a_turn_over_the_ceiling(tmp_path: Path) -> None:
    mgr = SessionManager(
        FakeCodingAgent(),
        FakeWorkspace(),
        str(tmp_path),
        session_disk_limit_mb=1,
        new_id=lambda: "s1",
    )
    s = await mgr.create("r")
    # FakeWorkspace clones nothing — materialize the checkout + a >1 MB file by hand.
    checkout = tmp_path / "s1"
    checkout.mkdir()
    (checkout / "big.bin").write_bytes(b"\0" * (2 * _MB))
    with pytest.raises(SessionError, match="over disk quota"):
        await mgr.run_turn(s.id, "x").__anext__()
    # Flagged for the UI, and the turn never started (status untouched, no slot taken).
    assert mgr.get(s.id).over_quota is True
    assert mgr.get(s.id).status == "ready"


async def test_disk_quota_allows_a_small_checkout(tmp_path: Path) -> None:
    mgr = SessionManager(
        FakeCodingAgent(),
        FakeWorkspace(),
        str(tmp_path),
        session_disk_limit_mb=10,
        new_id=lambda: "s1",
    )
    s = await mgr.create("r")
    checkout = tmp_path / "s1"
    checkout.mkdir()
    (checkout / "small.txt").write_text("ok")
    seen = [ev.type async for ev in mgr.run_turn(s.id, "x")]
    assert seen[-1] == "done"
    assert mgr.get(s.id).over_quota is False


async def test_reset_clears_the_over_quota_flag(tmp_path: Path) -> None:
    mgr = SessionManager(
        FakeCodingAgent(),
        FakeWorkspace(),
        str(tmp_path),
        session_disk_limit_mb=1,
        new_id=lambda: "s1",
    )
    s = await mgr.create("r")
    checkout = tmp_path / "s1"
    checkout.mkdir()
    (checkout / "big.bin").write_bytes(b"\0" * (2 * _MB))
    with pytest.raises(SessionError, match="over disk quota"):
        await mgr.run_turn(s.id, "x").__anext__()
    assert mgr.get(s.id).over_quota is True
    await mgr.reset(s.id)
    assert mgr.get(s.id).over_quota is False


def test_directory_size_mb_sums_files_and_tolerates_missing(tmp_path: Path) -> None:
    assert directory_size_mb(tmp_path / "nope") == 0  # not-yet-cloned reads as empty
    (tmp_path / "a.bin").write_bytes(b"\0" * (3 * _MB))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\0" * _MB)
    assert directory_size_mb(tmp_path) == 4  # recurses subdirs
