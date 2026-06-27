"""Interactive terminal: PTY mechanics, the WS route's auth gate, and the reaper
guard that keeps a session alive while a terminal is open.

The PTY itself is exercised here (a real shell in a tmp cwd); the WebSocket *pump*
(serve_terminal) is deploy-only (pragma: no cover), so these cover everything around
it — that the shell runs in the right cwd, that resize reaches the PTY, that the
upgrade is token-gated, and that an open terminal is treated as activity.
"""

from __future__ import annotations

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

from jcode_ctl.agent import FakeCodingAgent
from jcode_ctl.sessions import SessionManager
from jcode_ctl.terminal import _close_child, _set_winsize, model_env, spawn_shell
from jcode_ctl.workspace import FakeWorkspace


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
    pid, fd = spawn_shell(
        str(tmp_path), env_overrides=model_env("qwen3-coder-next-q8")
    )
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
        FakeCodingAgent(),
        FakeWorkspace(),
        "/work",
        now=lambda: clock["t"],
        new_id=lambda: "s1",
    )
    s = await mgr.create("r")
    clock["t"] += timedelta(hours=48)
    assert mgr.idle_sessions(ttl_seconds=3600) == [s.id]  # idle now

    mgr.terminal_opened(s.id)
    assert mgr.idle_sessions(ttl_seconds=3600) == []  # an open terminal is kept alive

    mgr.terminal_closed(s.id)
    clock["t"] += timedelta(hours=48)  # idle again once no terminal is open
    assert mgr.idle_sessions(ttl_seconds=3600) == [s.id]
