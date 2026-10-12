"""Per-session unprivileged identities (S2.1 of
``docs/plans/SECURITY_REVIEW_FIXES_PLAN.md``).

The control server runs as root and holds ``JCODE_TOKEN``. A session's shell — and
git, which runs a checkout's hooks, filters and fsmonitor — must not: as root, any of
them could read ``/proc/1/environ`` for the token, another session's checkout, or the
server's own files. So every session gets a uid/gid of its own from a fixed pool, owns
exactly two trees (its checkout and its ``$HOME``, both 0700), and everything it runs
drops to that uid before exec.

Two rules keep a recycled uid from inheriting a previous holder's files or processes:
a uid is never handed out while anything it owns still exists (a tree, a process, a
``/tmp`` entry), and on start the server clears every tree that isn't a live session —
sessions are in-memory, so after a restart none are.

Nothing here follows a symlink: every removal ``lstat``s first and ``shutil.rmtree``
walks by file descriptor on Linux, so a link a session planted is unlinked, never
traversed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("jcode_ctl.sandbox")

# The pool the image pre-registers in /etc/passwd + /etc/group (jcode/Dockerfile), so
# tools that look the user up (git's ident, Node's os.userInfo, ssh) find a name. Far
# above the image's own system/user ranges; test_sandbox pins the Dockerfile to it.
UID_LOW = 20000
UID_HIGH = 20999

# World-writable places a session can leave files in outside its own trees. Top level
# only: anything deeper sits under a directory some uid owns, and goes with it.
SHARED_TMP_DIRS: tuple[str, ...] = ("/tmp", "/var/tmp", "/dev/shm")

_KILL_ROUNDS = 10
_LISTEN = "0A"


class PoolExhaustedError(RuntimeError):
    """Every uid in the pool is held or still owns something."""


@dataclass(frozen=True)
class SessionUser:
    """The identity a session's shell and git run as, and its private ``$HOME``."""

    uid: int
    gid: int
    home: str


class UidPool:
    """Hands out uids round-robin, skipping any held by a live session and any the
    ``in_use`` probe says still owns something. Round-robin rather than lowest-free so a
    just-released uid is the last to come back, which keeps reuse rare even before the
    probe has to catch it."""

    def __init__(
        self,
        low: int = UID_LOW,
        high: int = UID_HIGH,
        *,
        in_use: Callable[[int], bool] = lambda _uid: False,
    ) -> None:
        self._low = low
        self._size = high - low + 1
        self._cursor = 0
        self._held: set[int] = set()
        self._in_use = in_use

    def acquire(self) -> int:
        for step in range(self._size):
            uid = self._low + (self._cursor + step) % self._size
            if uid in self._held or self._in_use(uid):
                continue
            self._held.add(uid)
            self._cursor = (uid - self._low + 1) % self._size
            return uid
        raise PoolExhaustedError("no free session uid — delete a session first")

    def release(self, uid: int) -> None:
        self._held.discard(uid)


def drop_privileges(user: SessionUser, tty_fd: int = 0) -> None:
    """Become ``user`` for good, in the forked child before exec. Order matters: the
    supplementary groups and gid can only be shed while still root, so they go first
    and ``setuid`` last. The tty is handed over first too, while ``fchown`` is still
    allowed. The final probe proves root can't be regained — a half-dropped child must
    not exec."""
    os.fchown(tty_fd, user.uid, -1)
    os.setgroups([])
    os.setgid(user.gid)
    os.setuid(user.uid)
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (
        user.uid,
        user.uid,
        user.gid,
        user.gid,
    ):
        raise OSError("privilege drop did not take")
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise OSError("privilege drop is reversible")


def _status_uids(proc: str, pid: str) -> tuple[int, ...]:
    """The four uids of a live process; none for a zombie, which runs nothing and
    holds nothing — it only waits for its parent to reap it, and signalling it again
    would spin the kill loop for nothing."""
    with open(f"{proc}/{pid}/status", encoding="ascii", errors="replace") as fh:
        for line in fh:
            if line.startswith("State:") and line.split()[1:2] == ["Z"]:
                return ()
            if line.startswith("Uid:"):
                return tuple(int(v) for v in line.split()[1:])
    return ()


def pids_of_uids(uids: Iterable[int] | range, proc: str = "/proc") -> list[int]:
    """Pids whose real, effective, saved or fs uid is one of ``uids``. Any of the four
    counts: a process that juggled its ids is still the session's."""
    wanted = set(uids)
    self_pid = os.getpid()
    found: list[int] = []
    try:
        entries = os.listdir(proc)
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit() or int(entry) == self_pid:
            continue
        try:
            ids = _status_uids(proc, entry)
        except (OSError, ValueError):
            continue
        if wanted.intersection(ids):
            found.append(int(entry))
    return found


def kill_uids(uids: Iterable[int] | range, proc: str = "/proc") -> list[int]:
    """SIGKILL every process running as one of ``uids`` — the stop/delete backstop that
    the cwd sweep can't be: a process that ``cd``'d out of the checkout, or a daemon
    double-forked out of the shell's group, is still the session's uid. Repeats a few
    rounds because a process can fork between the scan and the kill."""
    wanted = list(uids)
    killed: list[int] = []
    for _ in range(_KILL_ROUNDS):
        pids = pids_of_uids(wanted, proc)
        if not pids:
            break
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
    return killed


def listener_uids(port: int, proc_net: str = "/proc/net") -> set[int]:
    """The uids owning every TCP socket LISTENING on ``port`` (any address, v4 and v6).
    Two sessions share one network namespace, so another session can bind the same port
    on a different loopback address; the preview proxy uses this to refuse unless every
    listener on the port is the session's own."""
    owners: set[int] = set()
    for name in ("tcp", "tcp6"):
        try:
            with open(f"{proc_net}/{name}", encoding="ascii", errors="replace") as fh:
                lines = fh.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8 or fields[3] != _LISTEN:
                continue
            _, _, port_hex = fields[1].rpartition(":")
            try:
                if int(port_hex, 16) == port:
                    owners.add(int(fields[7]))
            except ValueError:
                continue
    return owners


def remove_entry(path: Path) -> None:
    """Remove ``path`` whatever it is, without following it if it is a symlink."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except OSError as exc:
        # Left behind means still owned: the pool's probe keeps its uid out of reuse.
        _log.warning("could not remove %s: %s", path, exc)


def ensure_root_dir(path: Path) -> None:
    """``/work`` and ``/work/.home``: root-owned 0711, so a session can reach its own
    tree by name but can neither list the others nor create anything beside them."""
    path.mkdir(parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a directory")
    os.chown(path, 0, 0, follow_symlinks=False)
    os.chmod(path, 0o711)


def make_private_dir(path: Path, user: SessionUser) -> None:
    """Create a directory owned by ``user``, mode 0700. ``mkdir`` without ``exist_ok``:
    an existing path here was not made for this session, so refusing is the safe
    answer."""
    os.mkdir(path, 0o700)
    os.chown(path, user.uid, user.gid, follow_symlinks=False)
    os.chmod(path, 0o700)


def sweep_orphans(root: Path, live: set[str], keep: set[str]) -> list[str]:
    """Remove every entry directly under ``root`` that isn't a live session or in
    ``keep``. Returns the names removed."""
    removed: list[str] = []
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return removed
    for name in names:
        if name in live or name in keep:
            continue
        remove_entry(root / name)
        removed.append(name)
    return removed


def purge_owned(
    uids: Iterable[int] | range, dirs: Iterable[str] = SHARED_TMP_DIRS
) -> None:
    """Remove top-level entries in the shared temp dirs owned by one of ``uids``, so a
    recycled uid can't read what the last session left in ``/tmp``."""
    wanted = set(uids)
    for base in dirs:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            path = Path(base) / name
            try:
                owner = os.lstat(path).st_uid
            except OSError:
                continue
            if owner in wanted:
                remove_entry(path)


def owns_anything(
    uid: int,
    roots: Iterable[Path],
    *,
    tmp_dirs: Iterable[str] = SHARED_TMP_DIRS,
    proc: str = "/proc",
) -> bool:
    """True while ``uid`` still owns a tree under a session root, a shared temp entry,
    or a running process — the pool's reuse guard."""
    for base in [*roots, *(Path(d) for d in tmp_dirs)]:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            try:
                if os.lstat(Path(base) / name).st_uid == uid:
                    return True
            except OSError:
                continue
    return bool(pids_of_uids([uid], proc))
