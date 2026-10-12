"""Per-session uids (S2.1): the pool never hands out a uid that still owns something,
the privilege drop is ordered and irreversible, processes are found and killed by uid,
the preview's port-ownership probe reads /proc/net, and removals never follow a
symlink."""

from __future__ import annotations

import os
import re
import resource
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import jcode_ctl.sandbox as sandbox_mod
from jcode_ctl.sandbox import (
    SESSION_NPROC,
    SHARED_TMP_DIRS,
    UID_HIGH,
    UID_LOW,
    PoolExhaustedError,
    SessionUser,
    UidPool,
    drop_privileges,
    ensure_root_dir,
    ipc_objects,
    kill_uids,
    listener_uids,
    make_private_dir,
    owns_anything,
    pids_of_uids,
    purge_owned,
    remove_entry,
    sweep_orphans,
)

_ROOT = os.geteuid() == 0
needs_root = pytest.mark.skipif(not _ROOT, reason="needs root to chown / setuid")
_JCODE = Path(__file__).resolve().parents[1]


# --- the pool ----------------------------------------------------------------------


def test_pool_hands_out_distinct_uids_round_robin() -> None:
    pool = UidPool(100, 102)
    assert [pool.acquire(), pool.acquire()] == [100, 101]
    pool.release(100)
    # Round-robin: the just-released uid comes back last, not next.
    assert pool.acquire() == 102
    assert pool.acquire() == 100
    with pytest.raises(PoolExhaustedError):
        pool.acquire()


def test_pool_never_reuses_a_uid_that_still_owns_something() -> None:
    # The leftover-tree case: a released uid whose checkout couldn't be removed (or that
    # left a process / a /tmp file) must not go to the next session.
    still_owns = {100}
    pool = UidPool(100, 101, in_use=lambda uid: uid in still_owns)
    assert pool.acquire() == 101
    with pytest.raises(PoolExhaustedError):
        pool.acquire()
    still_owns.clear()
    assert pool.acquire() == 100


def test_image_registers_exactly_the_pool() -> None:
    # The Dockerfile's passwd loop and the code's range must agree, or a session uid has
    # no passwd entry (git ident / os.userInfo fail).
    dockerfile = (_JCODE / "Dockerfile").read_text()
    m = re.search(r"seq (\d+) (\d+)\); do", dockerfile)
    assert m is not None
    assert (int(m.group(1)), int(m.group(2))) == (UID_LOW, UID_HIGH)


# --- the privilege drop --------------------------------------------------------------


@pytest.fixture
def rlimits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, tuple[int, int]]]:
    """Never lower this test process's own limits (irreversible for a non-root run)."""
    set_: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        sandbox_mod.resource, "setrlimit", lambda r, lim: set_.append((r, lim))
    )
    return set_


def test_drop_privileges_sheds_groups_then_gid_then_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    ids = {"uid": 0, "gid": 0}

    def _setuid(uid: int) -> None:
        if ids["uid"] != 0:
            raise PermissionError
        calls.append(("setuid", uid))
        ids["uid"] = uid

    def _setgid(gid: int) -> None:
        calls.append(("setgid", gid))
        ids["gid"] = gid

    monkeypatch.setattr(
        os, "fchown", lambda fd, u, g: calls.append(("fchown", (fd, u)))
    )
    monkeypatch.setattr(
        sandbox_mod.resource,
        "setrlimit",
        lambda r, lim: calls.append(("setrlimit", (r, lim))),
    )
    monkeypatch.setattr(os, "setgroups", lambda g: calls.append(("setgroups", g)))
    monkeypatch.setattr(os, "setgid", _setgid)
    monkeypatch.setattr(os, "setuid", _setuid)
    monkeypatch.setattr(os, "getuid", lambda: ids["uid"])
    monkeypatch.setattr(os, "geteuid", lambda: ids["uid"])
    monkeypatch.setattr(os, "getgid", lambda: ids["gid"])
    monkeypatch.setattr(os, "getegid", lambda: ids["gid"])
    drop_privileges(SessionUser(20001, 20001, "/h"), tty_fd=7)
    assert calls == [
        ("fchown", (7, 20001)),
        # The per-uid process cap, while root can still lower it for good.
        ("setrlimit", (resource.RLIMIT_NPROC, (SESSION_NPROC, SESSION_NPROC))),
        ("setgroups", []),
        ("setgid", 20001),
        ("setuid", 20001),
    ]


def test_drop_privileges_refuses_a_reversible_drop(
    monkeypatch: pytest.MonkeyPatch, rlimits: list[tuple[int, tuple[int, int]]]
) -> None:
    # If root could be regained (a broken setuid), the child must not exec the shell.
    for name in ("fchown", "setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda *_a: None)
    for name in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(os, name, lambda: 20001)
    with pytest.raises(OSError, match="reversible"):
        drop_privileges(SessionUser(20001, 20001, "/h"))


def test_drop_privileges_refuses_a_drop_that_did_not_take(
    monkeypatch: pytest.MonkeyPatch, rlimits: list[tuple[int, tuple[int, int]]]
) -> None:
    for name in ("fchown", "setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda *_a: None)
    for name in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(os, name, lambda: 0)
    with pytest.raises(OSError, match="did not take"):
        drop_privileges(SessionUser(20001, 20001, "/h"))


# --- processes by uid ----------------------------------------------------------------


def _fake_proc(root: Path, table: dict[int, tuple[int, ...]]) -> str:
    for pid, uids in table.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "status").write_text(
            "Name:\tx\nUid:\t{}\t{}\t{}\t{}\nGid:\t0\t0\t0\t0\n".format(*uids)
        )
    (root / "self").mkdir()
    return str(root)


def test_pids_of_uids_matches_any_of_the_four_ids(tmp_path: Path) -> None:
    proc = _fake_proc(
        tmp_path,
        {
            11: (20001, 20001, 20001, 20001),
            12: (0, 20001, 0, 0),  # effective only
            13: (0, 0, 20001, 0),  # saved only
            14: (20002, 20002, 20002, 20002),
            15: (0, 0, 0, 0),
        },
    )
    assert sorted(pids_of_uids([20001], proc)) == [11, 12, 13]


def test_a_zombie_is_not_a_live_process(tmp_path: Path) -> None:
    (tmp_path / "41").mkdir()
    (tmp_path / "41" / "status").write_text(
        "Name:\tbash\nState:\tZ (zombie)\nUid:\t20001\t20001\t20001\t20001\n"
    )
    assert pids_of_uids([20001], str(tmp_path)) == []


def test_kill_uids_signals_only_the_session_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _fake_proc(tmp_path, {21: (20001,) * 4, 22: (20001,) * 4, 23: (20002,) * 4})
    sent: list[tuple[int, int]] = []

    def _kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        shutil.rmtree(Path(proc) / str(pid))  # the process is gone once killed

    monkeypatch.setattr(os, "kill", _kill)
    assert sorted(kill_uids([20001], proc)) == [21, 22]
    assert sorted(sent) == [(21, signal.SIGKILL), (22, signal.SIGKILL)]


@needs_root
def test_kill_uids_kills_a_real_process_wherever_its_cwd() -> None:
    uid = 29_995
    victim = subprocess.Popen(
        ["sleep", "300"], cwd="/", user=uid, group=uid, extra_groups=[]
    )
    bystander = subprocess.Popen(["sleep", "300"])
    try:
        deadline = time.monotonic() + 5
        while victim.pid not in pids_of_uids([uid]):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert victim.pid in kill_uids([uid])
        assert victim.wait(timeout=5) == -signal.SIGKILL
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=5)


# --- port ownership -------------------------------------------------------------------

_TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)


def _tcp_line(addr: str, port: int, state: str, uid: int) -> str:
    return (
        f"   0: {addr}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000"
        f" 00000000 {uid:>5} 0 12345 1 0000000000000000 100 0 0 10 0\n"
    )


def test_listener_uids_reads_listening_sockets_on_both_families(
    tmp_path: Path,
) -> None:
    (tmp_path / "tcp").write_text(
        _TCP_HEADER
        + _tcp_line("0100007F", 5173, "0A", 20001)
        + _tcp_line("0100007F", 5173, "01", 20009)  # established, not a listener
        + _tcp_line("0100007F", 5174, "0A", 20002)  # another port
    )
    v6_loopback = "00000000000000000000000001000000"
    (tmp_path / "tcp6").write_text(
        _TCP_HEADER + _tcp_line(v6_loopback, 5173, "0A", 20003)
    )
    assert listener_uids(5173, str(tmp_path)) == {20001, 20003}
    assert listener_uids(5174, str(tmp_path)) == {20002}
    assert listener_uids(5199, str(tmp_path)) == set()


def test_listener_uids_tolerates_a_missing_table(tmp_path: Path) -> None:
    assert listener_uids(5173, str(tmp_path)) == set()


def test_listener_uids_sees_a_real_listener() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert listener_uids(port) == {os.getuid()}
    assert listener_uids(port) == set()


# --- trees ---------------------------------------------------------------------------


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    (d / "precious").write_text("keep")
    return d


def test_remove_entry_unlinks_a_symlink_without_following_it(
    tmp_path: Path, outside: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(outside)
    remove_entry(link)
    assert not link.is_symlink()
    assert (outside / "precious").read_text() == "keep"


def test_remove_entry_never_descends_through_a_planted_link(
    tmp_path: Path, outside: Path
) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "escape").symlink_to(outside)
    remove_entry(tree)
    assert not tree.exists()
    assert (outside / "precious").read_text() == "keep"


def test_remove_entry_missing_is_a_noop(tmp_path: Path) -> None:
    remove_entry(tmp_path / "nope")


def test_sweep_orphans_keeps_live_and_kept_names(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for name in ("live", "orphan", "keepme"):
        (root / name).mkdir(parents=True)
    (root / "stray").write_text("x")
    removed = sweep_orphans(root, {"live"}, {"keepme"})
    assert sorted(removed) == ["orphan", "stray"]
    assert sorted(os.listdir(root)) == ["keepme", "live"]
    assert sweep_orphans(tmp_path / "missing", set(), set()) == []


def test_purge_owned_removes_only_the_uids_entries(tmp_path: Path) -> None:
    me = os.getuid()
    (tmp_path / "mine").write_text("x")
    purge_owned([me + 1], dirs=[str(tmp_path)])
    assert (tmp_path / "mine").exists()
    purge_owned([me], dirs=[str(tmp_path), str(tmp_path / "missing")])
    assert not (tmp_path / "mine").exists()


def test_owns_anything_checks_trees_temp_and_processes(tmp_path: Path) -> None:
    procdir = tmp_path / "proc"
    procdir.mkdir()
    proc = _fake_proc(procdir, {31: (29_993,) * 4})
    empty = tmp_path / "empty"
    empty.mkdir()
    assert owns_anything(29_993, [empty], tmp_dirs=(), proc=proc)  # a process
    assert not owns_anything(29_994, [empty], tmp_dirs=(), proc=proc)
    (empty / "mine").write_text("x")
    me = os.getuid()
    assert owns_anything(me, [empty], tmp_dirs=(), proc=str(tmp_path / "noproc"))
    assert owns_anything(me, [], tmp_dirs=[str(empty)], proc=str(tmp_path / "noproc"))


@pytest.fixture
def base() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="jcode-sbx-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@needs_root
def test_root_dirs_are_root_0711_and_session_dirs_uid_0700(base: Path) -> None:
    root = base / "work"
    root.mkdir(mode=0o777)
    os.chown(root, 29_992, 29_992)  # e.g. a volume created by something else
    ensure_root_dir(root)
    st = os.lstat(root)
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (0, 0, 0o711)
    user = SessionUser(29_991, 29_991, str(root / "h"))
    make_private_dir(root / "s1", user)
    st = os.lstat(root / "s1")
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (29_991, 29_991, 0o700)
    with pytest.raises(FileExistsError):
        make_private_dir(root / "s1", user)


def test_ensure_root_dir_refuses_a_symlink(base: Path, monkeypatch: Any) -> None:
    target = base / "target"
    target.mkdir()
    (base / "work").symlink_to(target)
    monkeypatch.setattr(sandbox_mod.os, "chown", lambda *_a, **_k: None)
    with pytest.raises(OSError, match="not a directory"):
        ensure_root_dir(base / "work")


# --- SysV IPC and the other shared places --------------------------------------------


def _sysvipc(root: Path) -> str:
    (root / "shm").write_text(
        "       key      shmid perms                  size  cpid  lpid nattch   uid"
        "   gid  cuid  cgid      atime      dtime      ctime                   rss"
        "                  swap\n"
        "         0         11   600                  4096   100   100      0 20001"
        " 20001 20001 20001          0          0          0                     0"
        "                     0\n"
        "         0         12   600                  4096   100   100      0     0"
        "     0 20001 20001          0          0          0                     0"
        "                     0\n"  # given away, but created by the session
        "         0         13   600                  4096   100   100      0 20002"
        " 20002 20002 20002          0          0          0                     0"
        "                     0\n"
    )
    (root / "msg").write_text(
        "       key      msqid perms      cbytes       qnum lspid lrpid   uid   gid"
        "  cuid  cgid      stime      rtime      ctime\n"
        "         0         21   600           0          0     0     0 20001 20001"
        " 20001 20001          0          0          0\n"
    )
    (root / "sem").write_text(
        "       key      semid perms      nsems   uid   gid  cuid  cgid      otime"
        "      ctime\n"
        "         0         31   600          1 20001 20001 20001 20001          0"
        "          0\n"
    )
    return str(root)


def test_ipc_objects_finds_what_the_uid_owns_or_created(tmp_path: Path) -> None:
    ipc = _sysvipc(tmp_path)
    assert sorted(ipc_objects([20001], ipc)) == [
        ("msg", 21),
        ("sem", 31),
        ("shm", 11),
        ("shm", 12),
    ]
    assert ipc_objects([20002], ipc) == [("shm", 13)]
    assert ipc_objects([20001], str(tmp_path / "missing")) == []


def test_purge_owned_removes_the_uids_ipc_objects(tmp_path: Path) -> None:
    ipc = _sysvipc(tmp_path)
    removed: list[tuple[str, int]] = []
    purge_owned([20002], dirs=[], sysvipc=ipc, rmid=lambda k, i: removed.append((k, i)))
    assert removed == [("shm", 13)]


def test_an_ipc_object_keeps_its_uid_out_of_the_pool(tmp_path: Path) -> None:
    ipc = _sysvipc(tmp_path)
    assert owns_anything(20002, [], tmp_dirs=(), proc=str(tmp_path), sysvipc=ipc)
    assert not owns_anything(20003, [], tmp_dirs=(), proc=str(tmp_path), sysvipc=ipc)


@needs_root
def test_purge_owned_really_removes_a_shared_memory_segment() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    uid = 29_996
    # IPC_PRIVATE, one page, IPC_CREAT | 0600 — created by root, then handed to the uid
    # with IPC_SET (struct ipc_perm leads struct shmid_ds; key, uid, gid, cuid, cgid).
    shmid = libc.shmget(0, 4096, 0o1000 | 0o600)
    if shmid < 0:
        pytest.skip("SysV shm unavailable here")

    class _Perm(ctypes.Structure):
        _fields_ = [
            ("key", ctypes.c_int),
            ("uid", ctypes.c_uint),
            ("gid", ctypes.c_uint),
            ("cuid", ctypes.c_uint),
            ("cgid", ctypes.c_uint),
            ("mode", ctypes.c_ushort),
        ]

    try:
        perm_buf = (ctypes.c_char * 256)()
        ipc_stat, ipc_set = 2, 1
        assert libc.shmctl(shmid, ipc_stat, perm_buf) == 0
        perm = _Perm.from_buffer(perm_buf)
        perm.uid, perm.gid = uid, uid
        assert libc.shmctl(shmid, ipc_set, perm_buf) == 0
        assert ("shm", shmid) in ipc_objects([uid])
        purge_owned([uid], dirs=[])
        assert ("shm", shmid) not in ipc_objects([uid])
    finally:
        libc.shmctl(shmid, 0, None)


def test_the_shared_places_cover_lock_and_mqueue() -> None:
    assert {"/tmp", "/var/tmp", "/dev/shm", "/run/lock", "/dev/mqueue"} <= set(
        SHARED_TMP_DIRS
    )
