"""Interactive terminal: PTY mechanics, the WS route's auth gate, the reaper guard
that keeps a session alive while a terminal is open, and the shell-exit-vs-socket-drop
distinction that decides whether a session is paused.

The PTY itself is exercised here (a real shell in a tmp cwd); the WebSocket pump
(serve_terminal) is deploy-only (pragma: no cover) but the exit/drop branch is driven
end-to-end below with a fake socket + a real shell, since it's the subtle bit.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import select
import struct
import termios
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jcode_ctl.sessions import SessionManager
from jcode_ctl.terminal import (
    TerminalRegistry,
    _close_child,
    _set_winsize,
    kill_processes_in_dir,
    model_env,
    preview_env,
    serve_terminal,
    spawn_shell,
)
from jcode_ctl.workspace import FakeWorkspace


class _FakeWS:
    """A minimal stand-in for the Starlette WebSocket serve_terminal drives: it plays a
    scripted set of inbound messages, then blocks `receive()` until `close()` is called
    (mirroring a socket that disconnects once the server closes it). Records the bytes
    it was sent and whether it was closed (so a takeover is observable)."""

    def __init__(self, script: list[dict[str, object]]) -> None:
        self._script = list(script)
        self._closed = asyncio.Event()
        self.closed = False
        self.sent: list[bytes] = []

    async def receive(self) -> dict[str, object]:
        if self._script:
            return self._script.pop(0)
        await self._closed.wait()
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


async def _wait_sent(ws: _FakeWS, needle: bytes, timeout: float = 5.0) -> None:
    """Spin until ``needle`` shows up in the bytes ``ws`` was sent (output is async)."""
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if needle in b"".join(ws.sent):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{needle!r} never sent; got {b''.join(ws.sent)!r}")


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_serve_terminal_pauses_on_shell_exit(tmp_path) -> None:
    # The headline distinction: a real shell exit (the user types `exit` / Ctrl-D) EOFs
    # the PTY, so on_shell_exit fires → the route pauses the session.
    registry = TerminalRegistry()
    exited: list[int] = []
    ws = _FakeWS([{"type": "websocket.receive", "bytes": b"exit\n"}])
    try:
        await asyncio.wait_for(
            serve_terminal(
                ws,  # type: ignore[arg-type]
                "s1",
                registry,
                str(tmp_path),
                on_shell_exit=lambda pid: exited.append(pid),
            ),
            timeout=10,
        )
    finally:
        registry.close_all()
    assert exited  # the shell exit was detected and would pause the session
    assert registry.get("s1") is None  # an exited shell is dropped from the registry


async def test_serve_terminal_socket_drop_keeps_the_shell_running(tmp_path) -> None:
    # A browser socket drop (tab switch / leaving the app) must NOT pause the session —
    # the shell never exited, so on_shell_exit must not fire AND the PTY stays alive,
    # detached, in the registry so a reconnect reattaches to it.
    registry = TerminalRegistry()
    exited: list[int] = []
    ws = _FakeWS([{"type": "websocket.disconnect"}])
    try:
        await asyncio.wait_for(
            serve_terminal(
                ws,  # type: ignore[arg-type]
                "s1",
                registry,
                str(tmp_path),
                on_shell_exit=lambda pid: exited.append(pid),
            ),
            timeout=10,
        )
        assert exited == []
        term = registry.get("s1")
        assert term is not None and term.alive  # the detached shell is still running
        assert term.attached is None  # ...but nobody is driving it
    finally:
        registry.close_all()


async def test_shell_persists_across_reconnect_and_replays_scrollback(tmp_path) -> None:
    # The detached-shell promise: leave (disconnect), the shell keeps running, and a
    # reconnect reattaches to the SAME shell (no new pid) and replays the scrollback.
    registry = TerminalRegistry()
    pids: list[int] = []
    resize = '{"resize": {"rows": 40, "cols": 80}}'
    ws1 = _FakeWS(
        [
            {"type": "websocket.receive", "text": resize},
            {"type": "websocket.receive", "bytes": b"echo PERSIST_MARKER\n"},
        ]
    )
    t1 = asyncio.ensure_future(
        serve_terminal(
            ws1,  # type: ignore[arg-type]
            "s1",
            registry,
            str(tmp_path),
            model="qwen3-coder-next",
            preview_port=5173,
            on_open=lambda p: pids.append(p),
        )
    )
    try:
        await _wait_sent(ws1, b"PERSIST_MARKER")  # the command ran while attached
        await ws1.close()  # leave the app — disconnect the socket
        await asyncio.wait_for(t1, timeout=10)

        term = registry.get("s1")
        assert term is not None and term.alive  # shell survived the disconnect
        assert pids == [term.pid]

        ws2 = _FakeWS([])  # reconnect: a fresh xterm with no history of its own
        t2 = asyncio.ensure_future(
            serve_terminal(
                ws2,  # type: ignore[arg-type]
                "s1",
                registry,
                str(tmp_path),
                on_open=lambda p: pids.append(p),
            )
        )
        await _wait_sent(ws2, b"PERSIST_MARKER")  # the earlier output is replayed
        assert pids == [term.pid]  # reattached to the same shell — on_open not re-fired
        await ws2.close()
        await asyncio.wait_for(t2, timeout=10)
    finally:
        registry.close_all()


async def test_second_client_takes_over_and_closes_the_first(tmp_path) -> None:
    # One driver at a time: a second connect takes over the shell and closes the first
    # socket, so two browsers can't fight over one PTY.
    registry = TerminalRegistry()
    ws1 = _FakeWS([])  # connect and stay attached (no scripted disconnect)
    t1 = asyncio.ensure_future(
        serve_terminal(ws1, "s1", registry, str(tmp_path))  # type: ignore[arg-type]
    )
    try:
        await _wait_until(lambda: getattr(registry.get("s1"), "attached", None) is ws1)

        ws2 = _FakeWS([])
        t2 = asyncio.ensure_future(
            serve_terminal(ws2, "s1", registry, str(tmp_path))  # type: ignore[arg-type]
        )
        await asyncio.wait_for(t1, timeout=10)  # the takeover closed ws1 → t1 returns

        assert ws1.closed  # the first socket was closed by the takeover
        term = registry.get("s1")
        assert term is not None and term.attached is ws2  # the new client drives now
        await ws2.close()
        await asyncio.wait_for(t2, timeout=10)
    finally:
        registry.close_all()


def _read_until(fd: int, needle: bytes, timeout: float = 5.0) -> bytes:
    buf = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if needle in buf:
            break
    return buf


def test_spawn_shell_runs_in_the_session_cwd(tmp_path) -> None:
    (tmp_path / "needle.txt").write_text("x")
    pid, fd = spawn_shell(str(tmp_path))
    try:
        os.write(fd, b"ls\n")
        assert b"needle.txt" in _read_until(fd, b"needle.txt")
    finally:
        _close_child(pid, fd)


def test_spawn_shell_applies_model_env_overrides(tmp_path) -> None:
    # The session's model is pinned into the child env so the interactive `claude` CLI
    # never defaults to a cloud model the on-box gateway can't serve.
    pid, fd = spawn_shell(str(tmp_path), env_overrides=model_env("qwen3-coder-next-q8"))
    try:
        os.write(fd, b"echo M=$ANTHROPIC_MODEL H=$ANTHROPIC_DEFAULT_HAIKU_MODEL\n")
        out = _read_until(fd, b"M=qwen3-coder-next-q8")
        assert b"M=qwen3-coder-next-q8" in out
        assert b"H=qwen3-coder-next-q8" in out
    finally:
        _close_child(pid, fd)


def test_model_env_pins_every_tier() -> None:
    # All four tier aliases (opus/sonnet/haiku/fable) plus the main model resolve to the
    # one served route — the CLI must never request a tier the gateway doesn't have.
    env = model_env("qwen3-coder-next")
    assert set(env) == {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
    }
    assert all(v == "qwen3-coder-next" for v in env.values())


def test_preview_env_exports_the_port() -> None:
    # A $PORT-aware dev server (Next/CRA/Express…) binds the web-preview port, so a
    # server the owner or agent starts lands where the tunnel forwards.
    assert preview_env(5173) == {"PORT": "5173"}


def test_spawn_shell_applies_preview_env(tmp_path) -> None:
    # serve_terminal merges model_env + preview_env; a shell started with both sees PORT
    # alongside the model pins.
    overrides = {**model_env("qwen3-coder-next-q8"), **preview_env(5173)}
    pid, fd = spawn_shell(str(tmp_path), env_overrides=overrides)
    try:
        os.write(fd, b"echo P=$PORT M=$ANTHROPIC_MODEL\n")
        out = _read_until(fd, b"P=5173")
        assert b"P=5173" in out
        assert b"M=qwen3-coder-next-q8" in out
    finally:
        _close_child(pid, fd)


def test_set_winsize_reaches_the_pty(tmp_path) -> None:
    pid, fd = spawn_shell(str(tmp_path))
    try:
        _set_winsize(fd, 40, 120)
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (rows, cols) == (40, 120)
    finally:
        _close_child(pid, fd)


def test_terminal_rejects_a_bad_token(client: TestClient) -> None:
    # The upgrade is token-gated; a wrong bearer closes before accept (no shell spawns).
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(
            "/sessions/s1/terminal", headers={"Authorization": "Bearer wrong"}
        ),
    ):
        pass
    assert exc.value.code == 4401


def test_terminal_rejects_an_unknown_session(
    client: TestClient, auth: dict[str, str]
) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/sessions/nope/terminal", headers=auth),
    ):
        pass
    assert exc.value.code == 4404


async def test_open_terminal_is_activity_and_blocks_reaping() -> None:
    clock = {"t": datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)}
    mgr = SessionManager(
        FakeWorkspace(),
        "/work",
        now=lambda: clock["t"],
        new_id=lambda: "s1",
    )
    s = await mgr.create("r")
    clock["t"] += timedelta(hours=48)
    assert mgr.idle_sessions(ttl_seconds=3600) == [s.id]  # idle now

    mgr.terminal_opened(s.id, 4242)
    assert mgr.idle_sessions(ttl_seconds=3600) == []  # an open terminal is kept alive

    mgr.terminal_closed(s.id, 4242)
    clock["t"] += timedelta(hours=48)  # idle again once no terminal is open
    assert mgr.idle_sessions(ttl_seconds=3600) == [s.id]


def test_kill_processes_in_dir_hard_kills_a_running_process(tmp_path) -> None:
    # The guaranteed hard-kill: a process running with its cwd inside the checkout is
    # SIGKILLed (the mid-tool turn / its tool subprocess case), and a process OUTSIDE is
    # left untouched.
    import subprocess

    inside = subprocess.Popen(["sleep", "300"], cwd=str(tmp_path))
    outside = subprocess.Popen(
        ["sleep", "300"]
    )  # cwd = the test's cwd, not the checkout
    try:
        # Give the children a moment to exist in /proc with their cwd set.
        deadline = time.monotonic() + 5
        while inside.pid not in kill_processes_in_dir(str(tmp_path)):
            if time.monotonic() > deadline:
                raise AssertionError("inside process was not killed")
            time.sleep(0.05)
        assert inside.wait(timeout=5) is not None  # reaped → dead
        assert outside.poll() is None  # the outside process is untouched
    finally:
        outside.kill()
        outside.wait(timeout=5)


async def test_delete_kills_open_terminals() -> None:
    # Delete must stop everything in the sandbox before the checkout is removed: every
    # open shell's process group is killed.
    killed: list[int] = []
    import jcode_ctl.sessions as sessions_mod

    mgr = SessionManager(FakeWorkspace(), "/work", new_id=lambda: "s1")
    s = await mgr.create("r")
    mgr.terminal_opened(s.id, 9991)
    mgr.terminal_opened(s.id, 9992)

    orig = sessions_mod.kill_process_group
    sessions_mod.kill_process_group = killed.append  # type: ignore[assignment]
    try:
        await mgr.delete(s.id)
    finally:
        sessions_mod.kill_process_group = orig

    assert sorted(killed) == [9991, 9992]  # both shells' groups killed


async def test_stop_kills_open_terminals() -> None:
    # Stop (the shell-exit pause) also kills open shells' process groups, but keeps the
    # checkout — only the processes are halted.
    killed: list[int] = []
    import jcode_ctl.sessions as sessions_mod

    ws = FakeWorkspace()
    mgr = SessionManager(ws, "/work", new_id=lambda: "s1")
    s = await mgr.create("r")
    mgr.terminal_opened(s.id, 7001)

    orig = sessions_mod.kill_process_group
    sessions_mod.kill_process_group = killed.append  # type: ignore[assignment]
    try:
        mgr.stop(s.id)
    finally:
        sessions_mod.kill_process_group = orig

    assert killed == [7001]
    assert ws.removed == []  # the checkout is kept on a stop
