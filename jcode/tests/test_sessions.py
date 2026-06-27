"""Session-manager lifecycle: create / capacity / reset / delete / stop / restart."""

from __future__ import annotations

import pytest

from jcode_ctl.sessions import SessionError, SessionManager
from jcode_ctl.workspace import FakeWorkspace


def _mgr(workspace=None, **kw) -> SessionManager:
    counter = {"n": 0}

    def _id() -> str:
        counter["n"] += 1
        return f"s{counter['n']}"

    return SessionManager(
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


async def test_reset_and_delete() -> None:
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    await mgr.reset(s.id)
    assert ws.reset_paths and str(ws.reset_paths[0]) == "/work/s1"
    await mgr.delete(s.id)
    assert ws.removed and str(ws.removed[0]) == "/work/s1"
    with pytest.raises(SessionError, match="unknown"):
        mgr.get(s.id)


async def test_session_model_is_recorded() -> None:
    # The model chosen at create (the owner's Settings → LLM selection) is recorded on
    # the session so the terminal can pin the `claude` CLI to it.
    mgr = _mgr()
    s = await mgr.create("r", model="qwen3-coder-next")
    assert s.model == "qwen3-coder-next"


async def test_model_defaults_to_empty_when_unset() -> None:
    mgr = _mgr()
    s = await mgr.create("r")
    assert s.model == ""


# --- Stop / restart (the terminal-exit pause) -------------------------------------


async def test_stop_pauses_and_keeps_the_checkout() -> None:
    # Stopping a session marks it `stopped` but does NOT remove the checkout — the work
    # is preserved for a restart (the owner chose "keep checkout (pause)").
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    stopped = mgr.stop(s.id)
    assert stopped.status == "stopped"
    assert ws.removed == []  # checkout kept on disk
    assert mgr.get(s.id).status == "stopped"


async def test_stop_with_an_open_terminal_clears_it_and_pauses() -> None:
    # Stop SIGKILLs each open terminal's process group (best-effort; a non-existent pid
    # is suppressed) and drops the tracked terminal so the session reads paused, not
    # falsely "fresh". The pid kill itself is OS-level — here we assert the bookkeeping.
    mgr = _mgr()
    s = await mgr.create("r")
    mgr.terminal_opened(s.id, 4242)
    mgr.stop(s.id)
    assert mgr.get(s.id).status == "stopped"


async def test_stop_is_idempotent() -> None:
    mgr = _mgr()
    s = await mgr.create("r")
    mgr.stop(s.id)
    assert mgr.stop(s.id).status == "stopped"


async def test_restart_resumes_a_paused_session() -> None:
    mgr = _mgr()
    s = await mgr.create("r")
    mgr.stop(s.id)
    resumed = mgr.restart(s.id)
    assert resumed.status == "ready"
    assert mgr.get(s.id).status == "ready"


async def test_restart_unknown_is_404() -> None:
    mgr = _mgr()
    with pytest.raises(SessionError, match="unknown"):
        mgr.restart("nope")


async def test_opening_a_terminal_resumes_a_paused_session() -> None:
    # Attaching a terminal to a stopped session brings it back to ready (the shell-exit
    # pause is undone the moment a new shell connects).
    mgr = _mgr()
    s = await mgr.create("r")
    mgr.stop(s.id)
    mgr.terminal_opened(s.id, 99)
    assert mgr.get(s.id).status == "ready"


async def test_lifecycle_logs(caplog) -> None:
    import logging

    mgr = _mgr()
    with caplog.at_level(logging.INFO, logger="jcode_ctl.sessions"):
        s = await mgr.create("r")
        mgr.stop(s.id)
        mgr.restart(s.id)
    msgs = " ".join(r.message for r in caplog.records)
    assert "session create" in msgs
    assert "session stop" in msgs
    assert "session restart" in msgs
