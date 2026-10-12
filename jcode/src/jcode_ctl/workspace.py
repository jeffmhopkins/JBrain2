"""The workspace port: provision and tear down a per-session git checkout.

Isolated behind a port so the session logic is testable without git or a network
(:class:`FakeWorkspace`). The real implementation shells to ``git`` inside the
sandbox volume, as the session's own uid (``sandbox.py``); the clone is the only place
a remote is contacted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Protocol

from jcode_ctl.agent_files import AGENT_FILES, write_agent_files
from jcode_ctl.sandbox import (
    UID_HIGH,
    UID_LOW,
    SessionUser,
    ensure_root_dir,
    kill_uids,
    make_private_dir,
    owns_anything,
    purge_owned,
    remove_entry,
    sweep_orphans,
)

_log = logging.getLogger("jcode_ctl.workspace")


class WorkspaceError(RuntimeError):
    """A clone/checkout/reset failed — surfaced as a clean control-API error."""


# Only fetch-over-the-wire transports. git's ``ext::`` transport runs an arbitrary
# command AT CLONE TIME (before any sandbox boundary), and ``file://``/local paths
# would let the sandbox read arbitrary host-visible trees — both are refused. The
# repo and refs are caller-supplied (CreateSessionRequest), so they are validated
# before they reach a git argument.
_ALLOWED_SCHEMES = ("https://", "git://")


def validate_repo(repo: str) -> None:
    """Reject anything but an https://|git:// URL (empty = a scratch workspace)."""
    if not repo:
        return
    if "::" in repo or not repo.startswith(_ALLOWED_SCHEMES):
        raise WorkspaceError(
            "repo must be an https:// or git:// URL — ssh/scp, file://, and git's "
            "ext:: transport are refused"
        )


def validate_ref(name: str, *, field: str) -> None:
    """A branch/work-branch must not be parseable as a git option (leading '-')."""
    if name.startswith("-"):
        raise WorkspaceError(f"{field} may not start with '-'")


class Workspace(Protocol):
    """Owns the on-disk lifecycle of every session's checkout and $HOME."""

    def startup(self, live: set[str]) -> None: ...

    def uid_in_use(self, uid: int) -> bool: ...

    def prepare(self, path: Path, user: SessionUser) -> None: ...

    async def clone(
        self, path: Path, repo: str, branch: str, work_branch: str, user: SessionUser
    ) -> None: ...

    async def reset(self, path: Path, user: SessionUser) -> None: ...

    def remove(self, path: Path) -> None: ...

    def purge_uid(self, uid: int) -> None: ...


# Proxy settings git must keep (the sandbox's egress goes through them when set);
# nothing else of the control server's environment reaches a git run.
_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def git_env(user: SessionUser) -> dict[str, str]:
    """The whole environment of a git run: the session's own HOME (an inherited
    ``HOME=/root`` is unreadable to the uid and git dies reading its config), a PATH,
    the proxies, and no prompts — a clone that wants credentials fails instead of
    hanging. Built from nothing so the control token can't leak into a hook."""
    env = {
        "HOME": user.home,
        "PATH": os.environ.get("PATH", _DEFAULT_PATH),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in _PROXY_VARS:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


# Subdirectories of a fresh $HOME: the bin dir that leads PATH (where `jcode-grok
# upgrade` installs, shadowing the image's copy for this session only) and the npm
# prefix.
_HOME_SUBDIRS = (".local", ".local/bin", ".npm-global")


class GitWorkspace:
    """Real workspace: per-session trees owned by the session's uid, and ``git`` run as
    that uid. Git runs a checkout's hooks, filters and fsmonitor, all of which the
    session controls, so a root git inside a session tree would be a root shell."""

    def __init__(
        self,
        allowlist: list[str],
        *,
        workspace_root: str = "/work",
        home_root: str = "/work/.home",
        uids: range = range(UID_LOW, UID_HIGH + 1),
    ) -> None:
        self._allowlist = allowlist
        self._root = Path(workspace_root)
        self._home_root = Path(home_root)
        self._uids = uids

    def startup(self, live: set[str]) -> None:
        """Run once before any uid is handed out. Sessions don't survive a restart, so
        every tree on the volume is an orphan whose uid the pool will recycle; clearing
        them (and any process or temp file still owned by a pool uid) is what makes a
        recycled uid start empty. Then publish the agents' instructions as root."""
        kill_uids(self._uids)
        ensure_root_dir(self._root)
        ensure_root_dir(self._home_root)
        keep = {*AGENT_FILES}
        if self._home_root.parent == self._root:
            keep.add(self._home_root.name)
        swept = sweep_orphans(self._root, live, keep)
        swept += sweep_orphans(self._home_root, live, set())
        purge_owned(self._uids)
        write_agent_files(self._root)
        if swept:
            _log.info("removed %d orphaned session tree(s)", len(swept))

    def uid_in_use(self, uid: int) -> bool:
        return owns_anything(uid, (self._root, self._home_root))

    def prepare(self, path: Path, user: SessionUser) -> None:
        # Made empty and handed over before anything runs as the uid, so creating the
        # skeleton as root here never writes into a tree the session already controls.
        make_private_dir(path, user)
        home = Path(user.home)
        make_private_dir(home, user)
        for sub in _HOME_SUBDIRS:
            d = home / sub
            d.mkdir(mode=0o755)
            os.chown(d, user.uid, user.gid, follow_symlinks=False)

    async def _git(self, *args: str, cwd: Path, user: SessionUser) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            env=git_env(user),
            user=user.uid,
            group=user.gid,
            extra_groups=[],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip()
            raise WorkspaceError(f"git {args[0]} failed: {detail}")

    async def clone(
        self, path: Path, repo: str, branch: str, work_branch: str, user: SessionUser
    ) -> None:
        validate_repo(repo)
        validate_ref(branch, field="branch")
        validate_ref(work_branch, field="work_branch")
        # Defense in depth beyond validate_repo: forbid the ext/file transports at
        # the git layer too, and use ``--`` so a value can never be read as an option.
        deny = ("-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never")
        if repo:  # empty repo = a scratch workspace (no clone)
            # Into the empty directory prepare() made, so the checkout is the uid's from
            # the first byte.
            await self._git(
                *deny,
                "clone",
                "--branch",
                branch,
                "--depth",
                "1",
                "--",
                repo,
                str(path),
                cwd=path,
                user=user,
            )
            await self._git("checkout", "-b", work_branch, cwd=path, user=user)
        else:
            await self._git("init", "-b", work_branch or "main", cwd=path, user=user)

    async def reset(self, path: Path, user: SessionUser) -> None:
        # -x as well as -fd: also remove IGNORED files (node_modules, dist/, build and
        # log caches). Those are the usual cause of a checkout blowing the disk ceiling,
        # and a plain `clean -fd` leaves them — so without -x "reset to recover space"
        # wouldn't actually recover it. A reset is a throwaway sandbox going pristine;
        # the cost is the next build re-installs deps, which is the point of a reset.
        await self._git("reset", "--hard", cwd=path, user=user)
        await self._git("clean", "-fdx", cwd=path, user=user)

    def remove(self, path: Path) -> None:
        remove_entry(path)

    def purge_uid(self, uid: int) -> None:
        purge_owned([uid])


class FakeWorkspace:
    """In-memory workspace for tests: records lifecycle calls, touches no disk."""

    def __init__(self) -> None:
        self.cloned: list[tuple[str, str, str]] = []
        self.clone_users: list[SessionUser] = []
        self.reset_paths: list[Path] = []
        self.removed: list[Path] = []
        self.prepared_homes: list[Path] = []
        self.purged_uids: list[int] = []
        self.started: list[set[str]] = []
        self.busy_uids: set[int] = set()

    def startup(self, live: set[str]) -> None:
        self.started.append(set(live))

    def uid_in_use(self, uid: int) -> bool:
        return uid in self.busy_uids

    def prepare(self, path: Path, user: SessionUser) -> None:
        self.prepared_homes.append(Path(user.home))

    async def clone(
        self, path: Path, repo: str, branch: str, work_branch: str, user: SessionUser
    ) -> None:
        self.cloned.append((repo, branch, work_branch))
        self.clone_users.append(user)

    async def reset(self, path: Path, user: SessionUser) -> None:
        self.reset_paths.append(path)

    def remove(self, path: Path) -> None:
        self.removed.append(path)

    def purge_uid(self, uid: int) -> None:
        self.purged_uids.append(uid)
