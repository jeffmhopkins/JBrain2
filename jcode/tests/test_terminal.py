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
import json
import os
import select
import struct
import subprocess
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jcode_ctl.sessions import SessionManager
from jcode_ctl.terminal import (
    TerminalRegistry,
    _close_child,
    _set_winsize,
    home_env,
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


async def test_serve_terminal_applies_the_session_home(tmp_path) -> None:
    # The app→serve_terminal seam: passing home= must put the session's own $HOME into
    # the shell (HOME survives /etc/profile, unlike PATH), so per-session ~/.grok and
    # tools resolve under it. app.py drives this with sessions.home_for(sid).
    registry = TerminalRegistry()
    home = str(tmp_path / "h")
    ws = _FakeWS(
        [
            {"type": "websocket.receive", "bytes": b"echo HID=$HOME\n"},
            {"type": "websocket.receive", "bytes": b"exit\n"},
        ]
    )
    try:
        await asyncio.wait_for(
            serve_terminal(
                ws,  # type: ignore[arg-type]
                "s1",
                registry,
                str(tmp_path),
                home=home,
            ),
            timeout=10,
        )
    finally:
        registry.close_all()
    assert f"HID={home}".encode() in b"".join(ws.sent)


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
        os.write(
            fd,
            b"echo M=$ANTHROPIC_MODEL H=$ANTHROPIC_DEFAULT_HAIKU_MODEL G=$GROK_MODEL\n",
        )
        out = _read_until(fd, b"M=qwen3-coder-next-q8")
        assert b"M=qwen3-coder-next-q8" in out
        assert b"H=qwen3-coder-next-q8" in out
        assert b"G=qwen3-coder-next-q8" in out
    finally:
        _close_child(pid, fd)


def test_model_env_pins_every_tier() -> None:
    # All four Claude tier aliases (opus/sonnet/haiku/fable) + the main model, plus the
    # Grok CLI's GROK_MODEL and OpenClaw's OPENCLAW_MODEL, resolve to the one served
    # route: no CLI may ever request a tier the single-model gateway doesn't have.
    env = model_env("qwen3-coder-next")
    assert set(env) == {
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "GROK_MODEL",
        "OPENCLAW_MODEL",
    }
    assert all(v == "qwen3-coder-next" for v in env.values())


def test_home_env_sets_home_and_a_path_leading_tool_bin() -> None:
    # The per-session HOME + its private bin lead PATH, so a tool installed there
    # shadows the image's /usr/local/bin copy for this session only.
    env = home_env("/work/.home/s1")
    assert env["HOME"] == "/work/.home/s1"
    assert env["JCODE_TOOLS_BIN"] == "/work/.home/s1/.local/bin"
    assert env["NPM_CONFIG_PREFIX"] == "/work/.home/s1/.npm-global"
    # The tool bin and the npm-global bin come FIRST, ahead of the inherited PATH.
    assert env["PATH"].startswith(
        "/work/.home/s1/.local/bin:/work/.home/s1/.npm-global/bin:"
    )
    # The image's tools stay reachable as a fallback, but AFTER the session's bin.
    assert "/usr/local/bin" in env["PATH"]
    parts = env["PATH"].split(":")
    assert parts.index("/work/.home/s1/.local/bin") < parts.index("/usr/local/bin")


def test_spawn_shell_applies_home_env(tmp_path) -> None:
    # A shell started with home_env sees its own HOME and the tool-bin marker. (PATH
    # ORDERING isn't asserted here: `bash -l` runs /etc/profile, which on Debian resets
    # root's PATH — the per-session bin is re-prepended by the profile.d snippet in the
    # image, absent from the test env. That snippet is covered separately below.)
    home = str(tmp_path / "home" / "s1")
    pid, fd = spawn_shell(str(tmp_path), env_overrides=home_env(home))
    try:
        # Read to the LAST token's expanded value: the command line is echoed first
        # (with literal $HOME), so reading to TB's resolved path captures the output.
        os.write(fd, b"echo OUT HOME=$HOME TB=$JCODE_TOOLS_BIN END\n")
        out = _read_until(fd, f"TB={home}/.local/bin END".encode())
        assert f"HOME={home}".encode() in out
        assert f"TB={home}/.local/bin".encode() in out
    finally:
        _close_child(pid, fd)


def test_jcode_path_profile_snippet_leads_path_with_the_session_bin() -> None:
    # The actual PATH-leading guarantee: /etc/profile.d/jcode-path.sh re-prepends the
    # per-session dirs AFTER /etc/profile resets root's PATH. Source it with the markers
    # home_env sets and confirm the session bin (then the npm bin) lead PATH.
    snippet = Path(__file__).resolve().parents[1] / "jcode-path.sh"
    script = (
        f"JCODE_TOOLS_BIN=/t HOME=/h; PATH=/usr/local/bin:/bin; "
        f'. "{snippet}"; echo "$PATH"'
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "/t:/h/.npm-global/bin:/usr/local/bin:/bin"


def test_jcode_path_profile_snippet_is_a_noop_without_the_marker() -> None:
    # Image-wide safety: in a non-session shell ($JCODE_TOOLS_BIN unset) the snippet
    # must not touch PATH.
    snippet = Path(__file__).resolve().parents[1] / "jcode-path.sh"
    script = f'unset JCODE_TOOLS_BIN; PATH=/usr/bin; . "{snippet}"; echo "$PATH"'
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "/usr/bin"


def _render_openclaw_config(
    home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # Source /etc/profile.d/openclaw-config.sh the way `bash -l` does, with a fake
    # `openclaw` on PATH so the `command -v openclaw` guard fires. Returns the completed
    # process; the rendered config lands at <home>/.openclaw/openclaw.json.
    hook = Path(__file__).resolve().parents[1] / "openclaw-config.sh"
    fakebin = home / "fakebin"
    fakebin.mkdir(parents=True)
    (fakebin / "openclaw").write_text("#!/bin/sh\nexit 0\n")
    (fakebin / "openclaw").chmod(0o755)
    full = {"HOME": str(home), "PATH": f"{fakebin}:/usr/bin:/bin", **env}
    return subprocess.run(
        ["bash", "-c", f'. "{hook}"'], capture_output=True, text=True, env=full
    )


def test_openclaw_config_hook_renders_valid_json_at_the_gateway(tmp_path) -> None:
    # The hook writes ~/.openclaw/openclaw.json from the OPENCLAW_* env: valid JSON, the
    # custom OpenAI-compatible provider at the gateway, and the per-session model pin as
    # the default (provider/model-id), so `openclaw` targets the on-box coder.
    home = tmp_path / "home"
    res = _render_openclaw_config(
        home,
        {
            "OPENCLAW_MODEL": "qwen3-coder-next-q8",
            "OPENCLAW_MODELS_BASE_URL": "http://local-llm:8080/v1",
            "OPENCLAW_API_KEY": "sk-local-noauth",
            "OPENCLAW_CONTEXT_WINDOW": "262144",
        },
    )
    assert res.returncode == 0, res.stderr
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    assert primary == "on-box-coder/qwen3-coder-next-q8"
    provider = cfg["models"]["providers"]["on-box-coder"]
    assert provider["baseUrl"] == "http://local-llm:8080/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "sk-local-noauth"
    model = provider["models"][0]
    # The per-session quant, not a cloud default:
    assert model["id"] == "qwen3-coder-next-q8"
    # An int, not a string — so it parses as JSON:
    assert model["contextWindow"] == 262144
    # gateway.mode=local is required or OpenClaw refuses to start its local gateway; the
    # port is an int (the loopback the on-demand `jcode-openclaw gateway` daemon binds).
    assert cfg["gateway"]["mode"] == "local"
    assert cfg["gateway"]["port"] == 18789


def test_openclaw_config_hook_honours_a_custom_gateway_port(tmp_path) -> None:
    # A per-session OPENCLAW_GATEWAY_PORT (for concurrent sessions sharing the container
    # loopback) lands as an int in gateway.port so each gateway can bind its own.
    home = tmp_path / "home"
    res = _render_openclaw_config(home, {"OPENCLAW_GATEWAY_PORT": "18801"})
    assert res.returncode == 0, res.stderr
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    assert cfg["gateway"] == {"mode": "local", "port": 18801}


def test_openclaw_config_hook_defaults_when_env_is_absent(tmp_path) -> None:
    # With no OPENCLAW_* env the hook still renders valid JSON pinned to the on-box
    # coder (the compose defaults), so a bare image never points `openclaw` at a cloud.
    home = tmp_path / "home"
    res = _render_openclaw_config(home, {})
    assert res.returncode == 0, res.stderr
    cfg = json.loads((home / ".openclaw" / "openclaw.json").read_text())
    provider = cfg["models"]["providers"]["on-box-coder"]
    primary = cfg["agents"]["defaults"]["model"]["primary"]
    assert primary == "on-box-coder/qwen3-coder-next"
    assert provider["baseUrl"] == "http://local-llm:8080/v1"


def test_openclaw_config_hook_is_a_noop_without_the_cli(tmp_path) -> None:
    # Image-wide safety: with `openclaw` not installed the guard skips, writing
    # nothing — the hook is harmless in any shell that doesn't have the CLI.
    home = tmp_path / "home"
    home.mkdir()
    hook = Path(__file__).resolve().parents[1] / "openclaw-config.sh"
    res = subprocess.run(
        ["bash", "-c", f'. "{hook}"'],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert res.returncode == 0, res.stderr
    assert not (home / ".openclaw").exists()


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
