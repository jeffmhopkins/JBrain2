"""The workspace: per-session trees owned by the session's uid, git run AS that uid
(never root inside a tree the session controls), the startup sweep of orphaned trees,
and the repo/ref validators that keep caller input out of git's option/transport
surface (the ext::/file:// and leading-dash vectors)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import jcode_ctl.sandbox as sandbox_mod
import jcode_ctl.workspace as workspace_mod
from jcode_ctl.agent_files import AGENTS_MD
from jcode_ctl.sandbox import SessionUser
from jcode_ctl.workspace import (
    GitWorkspace,
    WorkspaceError,
    git_env,
    validate_ref,
    validate_repo,
)

_ROOT = os.geteuid() == 0
needs_root = pytest.mark.skipif(not _ROOT, reason="needs root to chown / setuid")
# Real-uid tests use ids outside the session pool so nothing else in the suite (which
# allocates from the pool) can collide with them.
_UID = 29990


@pytest.fixture
def chown_ok(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Without root, record chowns instead of failing, so the tree logic still runs."""
    calls: list[tuple[str, int, int]] = []
    if not _ROOT:

        def _fake(path: Any, uid: int, gid: int, **_: Any) -> None:
            calls.append((str(path), uid, gid))

        monkeypatch.setattr(os, "chown", _fake)
    return calls


@pytest.fixture
def base() -> Iterator[Path]:
    """A 0711 directory directly under /tmp: a dropped uid must be able to reach a tree
    beneath it by name, which pytest's 0700 tmp_path parents would forbid."""
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="jcode-ws-"))
    d.chmod(0o711)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _user(root: Path, sid: str = "s1", uid: int = _UID) -> SessionUser:
    return SessionUser(uid=uid, gid=uid, home=str(root / ".home" / sid))


def test_prepare_makes_both_trees_private_to_the_session_uid(
    base: Path, chown_ok: list[tuple[str, int, int]]
) -> None:
    ws = GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home"))
    (base / ".home").mkdir(mode=0o711)
    user = _user(base)
    ws.prepare(base / "s1", user)
    home = Path(user.home)
    for tree in (base / "s1", home):
        assert stat.S_IMODE(os.lstat(tree).st_mode) == 0o700
    # The skeleton the tool-version mechanism needs: a PATH-leading bin + npm prefix.
    assert (home / ".local" / "bin").is_dir()
    assert (home / ".npm-global").is_dir()
    owned = [
        base / "s1",
        home,
        home / ".local",
        home / ".local/bin",
        home / ".npm-global",
    ]
    if _ROOT:
        for p in owned:
            assert (os.lstat(p).st_uid, os.lstat(p).st_gid) == (_UID, _UID)
    else:
        assert {Path(c[0]) for c in chown_ok} == set(owned)
        assert {(c[1], c[2]) for c in chown_ok} == {(_UID, _UID)}


def test_prepare_refuses_a_path_that_already_exists(
    base: Path, chown_ok: list[tuple[str, int, int]]
) -> None:
    # An existing path wasn't made for this session; it is never adopted.
    ws = GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home"))
    (base / "s1").mkdir()
    with pytest.raises(FileExistsError):
        ws.prepare(base / "s1", _user(base))


class _Recorder:
    """Stands in for asyncio.create_subprocess_exec: records argv + kwargs, exits 0."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        return _Proc()


async def test_every_git_run_is_the_session_uid_with_its_own_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Git runs a checkout's hooks/filters/fsmonitor — session-controlled code — so it
    # must never be root, and must see none of the server's environment.
    monkeypatch.setenv("JCODE_TOKEN", "secret-control-token")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    monkeypatch.setenv("no_proxy", "api")
    rec = _Recorder()
    monkeypatch.setattr(workspace_mod.asyncio, "create_subprocess_exec", rec)
    ws = GitWorkspace([])
    user = SessionUser(uid=20005, gid=20005, home="/work/.home/s1")
    await ws.clone(
        Path("/work/s1"), "https://github.com/me/r", "main", "jcode/s1", user
    )
    await ws.clone(Path("/work/s2"), "", "main", "jcode/s2", user)
    await ws.reset(Path("/work/s1"), user)
    verbs = [
        next(a for a in argv[1:] if not a.startswith("-") and "=" not in a)
        for argv, _ in rec.calls
    ]
    assert verbs == ["clone", "checkout", "init", "reset", "clean"]
    for argv, kw in rec.calls:
        assert argv[0] == "git"
        assert (kw["user"], kw["group"], kw["extra_groups"]) == (20005, 20005, [])
        env = kw["env"]
        assert env["HOME"] == "/work/.home/s1"
        assert "JCODE_TOKEN" not in env
        assert env["HTTPS_PROXY"] == "http://egress:3128"
        assert env["no_proxy"] == "api"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "safe.directory" not in " ".join(argv)


def test_git_env_is_built_from_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JCODE_TOKEN", "x")
    monkeypatch.setenv("GROK_API_KEY", "y")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    env = git_env(SessionUser(1, 1, "/h"))
    assert set(env) <= {
        "HOME",
        "PATH",
        "LANG",
        "GIT_TERMINAL_PROMPT",
        *workspace_mod._PROXY_VARS,
    }
    assert "HTTP_PROXY" not in env  # unset stays unset, not ""


@needs_root
async def test_real_git_runs_as_the_session_uid(base: Path) -> None:
    ws = GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home"))
    (base / ".home").mkdir(mode=0o711)
    user = _user(base)
    path = base / "s1"
    ws.prepare(path, user)
    await ws.clone(path, "", "main", "work", user)
    (path / "f.txt").write_text("x")
    os.chown(path / "f.txt", _UID, _UID)
    await ws.reset(path, user)
    assert not (path / "f.txt").exists()  # clean -fdx ran
    # Everything git wrote is the session's, not root's.
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames]:
            assert os.lstat(Path(dirpath) / name).st_uid == _UID, name
    assert os.lstat(path / ".git").st_uid == _UID


@needs_root
async def test_a_hook_in_the_checkout_runs_unprivileged(base: Path) -> None:
    # The F5 vector: a session plants a hook and waits for the server's next git run.
    ws = GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home"))
    (base / ".home").mkdir(mode=0o711)
    user = _user(base)
    path = base / "s1"
    ws.prepare(path, user)
    await ws.clone(path, "", "main", "work", user)
    # core.fsmonitor names a program git runs whenever it reads the index — as reset
    # does — so it is the session-controlled code a root git would have executed.
    proof = Path(user.home) / "fsmonitor-uid"
    monitor = Path(user.home) / "monitor.sh"
    monitor.write_text(f"#!/bin/sh\nid -u > {proof}\nexit 1\n")
    monitor.chmod(0o755)
    as_user: dict[str, Any] = {
        "check": True,
        "user": _UID,
        "group": _UID,
        "extra_groups": [],
        "env": {
            **git_env(user),
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": "a@b",
            "GIT_COMMITTER_NAME": "a",
            "GIT_COMMITTER_EMAIL": "a@b",
        },
    }
    git = ["git", "-C", str(path)]
    subprocess.run([*git, "commit", "--allow-empty", "-qm", "x"], **as_user)
    subprocess.run([*git, "config", "core.fsmonitor", str(monitor)], **as_user)
    await ws.reset(path, user)
    assert proof.read_text().strip() == str(_UID)


def _startup_ws(base: Path, monkeypatch: pytest.MonkeyPatch) -> GitWorkspace:
    # The sweep's process kill and /tmp purge act on the real system: point them at
    # nothing so the test only exercises the trees.
    monkeypatch.setattr(workspace_mod, "kill_uids", lambda _uids: [])
    monkeypatch.setattr(workspace_mod, "purge_owned", lambda _uids: None)
    return GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home"))


def test_startup_removes_every_tree_that_is_not_a_live_session(
    base: Path, monkeypatch: pytest.MonkeyPatch, chown_ok: list[tuple[str, int, int]]
) -> None:
    outside = base.parent / f"{base.name}-outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    try:
        (base / "live").mkdir()
        (base / "orphan").mkdir()
        (base / "orphan" / "escape").symlink_to(outside)  # inside an orphan tree
        (base / "orphan-file").write_text("x")
        (base / "link").symlink_to(outside)  # at the top level
        (base / ".home").mkdir()
        (base / ".home" / "live").mkdir()
        (base / ".home" / "orphan").mkdir()
        (base / ".home" / "link").symlink_to(outside)
        _startup_ws(base, monkeypatch).startup({"live"})
        assert sorted(os.listdir(base)) == [".home", "AGENTS.md", "CLAUDE.md", "live"]
        assert sorted(os.listdir(base / ".home")) == ["live"]
        # Never followed: the symlinks' target is untouched.
        assert (outside / "precious").read_text() == "keep"
    finally:
        shutil.rmtree(outside)


def test_startup_publishes_the_agent_files_and_locks_the_roots(
    base: Path, monkeypatch: pytest.MonkeyPatch, chown_ok: list[tuple[str, int, int]]
) -> None:
    base.chmod(0o777)
    _startup_ws(base, monkeypatch).startup(set())
    assert (base / "AGENTS.md").read_text() == AGENTS_MD
    assert (base / "CLAUDE.md").read_text() == AGENTS_MD
    for root in (base, base / ".home"):
        assert stat.S_IMODE(os.lstat(root).st_mode) == 0o711
        if _ROOT:
            assert os.lstat(root).st_uid == 0


def test_startup_clears_processes_and_temp_files_across_the_whole_pool(
    base: Path, monkeypatch: pytest.MonkeyPatch, chown_ok: list[tuple[str, int, int]]
) -> None:
    killed: list[range] = []
    purged: list[range] = []
    monkeypatch.setattr(workspace_mod, "kill_uids", killed.append)
    monkeypatch.setattr(workspace_mod, "purge_owned", purged.append)
    GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home")).startup(
        set()
    )
    pool = range(sandbox_mod.UID_LOW, sandbox_mod.UID_HIGH + 1)
    assert killed == [pool] and purged == [pool]


@needs_root
def test_uid_in_use_sees_a_leftover_tree(base: Path) -> None:
    ws = GitWorkspace([], workspace_root=str(base), home_root=str(base / ".home"))
    assert not ws.uid_in_use(29_986)
    (base / "left").mkdir()
    os.chown(base / "left", 29_986, 29_986)
    assert ws.uid_in_use(29_986)
    assert not ws.uid_in_use(29_987)


def test_empty_repo_is_a_scratch_workspace() -> None:
    validate_repo("")  # no raise


@pytest.mark.parametrize("repo", ["https://github.com/me/r", "git://host/r"])
def test_allowed_schemes(repo: str) -> None:
    validate_repo(repo)  # no raise


@pytest.mark.parametrize(
    "repo",
    [
        "ext::sh -c 'touch /tmp/pwned'",
        "file:///etc/passwd",
        "git@github.com:me/r",  # scp-like ssh
        "/home/user/secret",  # local path
        "-oProxyCommand=evil",  # leading dash
        "https://host/r::weird",  # embedded ::
    ],
)
def test_rejected_repos(repo: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_repo(repo)


@pytest.mark.parametrize("ref", ["-x", "--upload-pack=evil"])
def test_option_like_refs_rejected(ref: str) -> None:
    with pytest.raises(WorkspaceError):
        validate_ref(ref, field="branch")


def test_normal_ref_ok() -> None:
    validate_ref("main", field="branch")
    validate_ref("jcode/spike", field="work_branch")
