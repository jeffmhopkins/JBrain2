"""Session-manager lifecycle: create / capacity / reset / delete / stop / restart."""

from __future__ import annotations

import pytest

from jcode_ctl.sandbox import UidPool
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


async def test_create_provisions_a_per_session_home() -> None:
    # Each session gets its own $HOME (its tool bin on PATH, ~/.grok, npm prefix), under
    # the home_root sibling of the checkouts — not the checkout itself.
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    await mgr.create("r")
    assert ws.prepared_homes == [mgr.home_for("s1")]
    assert str(mgr.home_for("s1")) == "/work/.home/s1"


async def test_delete_purges_home_alongside_the_checkout() -> None:
    # A deleted session leaves nothing on the volume: both the checkout and its private
    # HOME (installed tools, ~/.grok, npm cache) are removed.
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    await mgr.delete(s.id)
    removed = {str(p) for p in ws.removed}
    assert removed == {"/work/s1", "/work/.home/s1"}


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
    # the session so the terminal can pin the `grok` CLI to it.
    mgr = _mgr()
    s = await mgr.create("r", model="qwen3-coder-next")
    assert s.model == "qwen3-coder-next"


async def test_model_defaults_to_empty_when_unset() -> None:
    mgr = _mgr()
    s = await mgr.create("r")
    assert s.model == ""


async def test_records_the_internet_optins() -> None:
    # The owner's per-session internet opt-ins are recorded on the session (fixed at
    # create) and surface in public() so the terminal exports them (docs plan).
    mgr = _mgr()
    s = await mgr.create("r", internet_search=True)
    assert s.internet_search is True and s.internet_egress is False
    pub = s.public()
    assert pub["internet_search"] is True and pub["internet_egress"] is False


async def test_internet_optins_default_off() -> None:
    mgr = _mgr()
    s = await mgr.create("r")
    assert s.internet_search is False and s.internet_egress is False


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


async def test_stop_with_an_open_terminal_clears_it_and_pauses(monkeypatch) -> None:
    # Stop SIGKILLs each open terminal's process group and drops the tracked terminal
    # so the session reads paused, not falsely "fresh". The kill is stubbed: 4242 is a
    # made-up pid, and on a busy machine a real process (even this test run's own
    # group) can hold it.
    import jcode_ctl.sessions as sessions_mod

    killed: list[int] = []
    monkeypatch.setattr(sessions_mod, "kill_process_group", killed.append)
    mgr = _mgr()
    s = await mgr.create("r")
    mgr.terminal_opened(s.id, 4242)
    mgr.stop(s.id)
    assert mgr.get(s.id).status == "stopped"
    assert killed == [4242]


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


# --- Per-session uids (S2.1) ------------------------------------------------------


async def test_each_session_gets_its_own_uid_and_home() -> None:
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    a = await mgr.create("r")
    b = await mgr.create("r")
    ua, ub = mgr.user_for(a.id), mgr.user_for(b.id)
    assert ua.uid != ub.uid
    assert ua.gid == ua.uid
    assert ua.home == "/work/.home/s1"
    # The clone ran as the session's user, not root.
    assert ws.clone_users == [ua, ub]
    # The uid stays off the session shape the api mirrors.
    assert "uid" not in a.public()


async def test_a_uid_still_owning_something_is_skipped() -> None:
    ws = FakeWorkspace()
    ws.busy_uids = {20000}
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    assert mgr.user_for(s.id).uid == 20001


async def test_delete_kills_by_uid_purges_and_frees_the_uid(monkeypatch) -> None:
    import jcode_ctl.sessions as sessions_mod

    killed: list[list[int]] = []
    monkeypatch.setattr(sessions_mod, "kill_uids", killed.append)
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    uid = mgr.user_for(s.id).uid
    await mgr.delete(s.id)
    assert killed == [[uid]]
    assert ws.purged_uids == [uid]
    with pytest.raises(SessionError):
        mgr.user_for(s.id)


async def test_stop_kills_by_uid_and_keeps_the_uid(monkeypatch) -> None:
    import jcode_ctl.sessions as sessions_mod

    killed: list[list[int]] = []
    monkeypatch.setattr(sessions_mod, "kill_uids", killed.append)
    mgr = _mgr()
    s = await mgr.create("r")
    uid = mgr.user_for(s.id).uid
    mgr.stop(s.id)
    assert killed == [[uid]]
    assert mgr.user_for(s.id).uid == uid  # a paused session resumes as itself


async def test_a_failed_create_leaves_no_tree_and_frees_the_uid() -> None:
    class FailOnce(FakeWorkspace):
        failed = False

        async def clone(self, path, repo, branch, work_branch, user) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("clone failed")
            await super().clone(path, repo, branch, work_branch, user)

    ws = FailOnce()
    mgr = _mgr(workspace=ws, uid_pool=UidPool(30000, 30000))
    with pytest.raises(RuntimeError):
        await mgr.create("r")
    assert {str(p) for p in ws.removed} == {"/work/s1", "/work/.home/s1"}
    assert mgr.list() == []
    s = await mgr.create("r")  # the one-uid pool's uid came back
    assert mgr.user_for(s.id).uid == 30000


async def test_a_failed_prepare_removes_nothing_it_did_not_make() -> None:
    # prepare refuses an existing path (it undoes its own partial work); the manager
    # must not then delete that path — it was never this session's.
    class Refuses(FakeWorkspace):
        def prepare(self, path, user) -> None:
            raise FileExistsError(path)

    ws = Refuses()
    mgr = _mgr(workspace=ws, uid_pool=UidPool(30000, 30000))
    with pytest.raises(FileExistsError):
        await mgr.create("r")
    assert ws.removed == []
    ws2 = FakeWorkspace()
    mgr._workspace = ws2  # the uid was released: the next create gets it
    s = await mgr.create("r")
    assert mgr.user_for(s.id).uid == 30000


async def test_a_delete_that_fails_midway_keeps_the_session_for_a_retry() -> None:
    class Flaky(FakeWorkspace):
        fail = True

        def remove(self, path) -> None:
            if self.fail:
                raise OSError("busy")
            super().remove(path)

    ws = Flaky()
    mgr = _mgr(workspace=ws)
    s = await mgr.create("r")
    with pytest.raises(OSError):
        await mgr.delete(s.id)
    assert mgr.user_for(s.id).uid  # still on the books, uid not freed
    ws.fail = False
    await mgr.delete(s.id)
    with pytest.raises(SessionError):
        mgr.get(s.id)


async def test_startup_hands_the_live_set_to_the_workspace() -> None:
    ws = FakeWorkspace()
    mgr = _mgr(workspace=ws)
    mgr.startup()
    assert ws.started == [set()]
