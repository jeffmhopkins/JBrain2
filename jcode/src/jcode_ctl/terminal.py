"""Interactive PTY terminal: a real shell in a session's checkout, over a WebSocket.

The control server forks a login shell under a pseudo-terminal rooted in the
session's workspace and bridges it to the WebSocket — keystrokes/paste in, raw
terminal bytes out, plus a resize control message. The api proxies this to the
owner's browser (xterm.js); this server stays internal + token-authed.

Safe by construction: the sandbox is an isolated, throwaway per-session checkout
on its own network (no host, no notes, no other services), so a shell in it — and
the ``claude`` CLI the owner runs inside it — can do no more than that isolated
checkout allows. The child inherits the process env (so ``claude``, git, etc.
resolve the on-box gateway) plus a real ``TERM``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import signal
import struct
import termios
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import WebSocket

_log = logging.getLogger("jcode_ctl.terminal")

# A login bash so the sandbox's profile (PATH, the agent's env) is in effect.
_SHELL: tuple[str, ...] = ("/bin/bash", "-l")
_READ_BYTES = 65536


def model_env(model: str) -> dict[str, str]:
    """Env that pins every model tier the interactive ``claude`` CLI might pick to the
    session's on-box model. Without it the CLI defaults to a cloud model
    (``claude-opus-4-…``) the local gateway has no route for, and every session errors
    "the selected model may not exist". The CLI resolves its ``/model`` aliases
    (opus/sonnet/haiku/fable) and its background summariser through these vars, so on a
    single-model gateway they must ALL map to the one served route. This pins the model
    for the interactive shell's ``claude`` CLI."""
    return {
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
    }


def preview_env(port: int) -> dict[str, str]:
    """Point ``$PORT``-respecting dev servers (Next.js, CRA, Astro, ``process.env.PORT``
    Express apps, …) at the web-preview port, so a server the owner or agent starts
    lands where the preview tunnel forwards (``cloudflared … localhost:<port>``). The
    tunnel and this var are fed the SAME ``preview_default_port`` so they can't drift; a
    framework that ignores ``$PORT`` (e.g. Vite, already on 5173) is unaffected.
    Loopback-only is fine — the tunnel runs in this same container."""
    return {"PORT": str(port)}


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Push the browser terminal's size onto the PTY so full-screen TUIs (vim, and
    the ``claude`` CLI itself) lay out correctly. Clamped to sane bounds."""
    rows = max(1, min(rows, 1000))
    cols = max(1, min(cols, 1000))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def spawn_shell(
    cwd: str,
    argv: tuple[str, ...] = _SHELL,
    *,
    env_overrides: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Fork ``argv`` under a new PTY in ``cwd`` and return ``(pid, master_fd)``.

    The child becomes its own session leader with the slave as its controlling
    terminal (``pty.fork`` does this), chdirs into the checkout, and execs the
    shell with the inherited env plus ``TERM`` and any ``env_overrides`` (the
    session's model pins). The parent drives the master fd.
    """
    pid, master_fd = pty.fork()
    if pid == 0:  # child — replace ourselves with the shell  # pragma: no cover
        try:
            os.chdir(cwd)
            env = dict(os.environ)
            env.setdefault("TERM", "xterm-256color")
            if env_overrides:
                env.update(env_overrides)
            os.execvpe(argv[0], list(argv), env)
        except Exception:  # exec failed — never fall back into Python in the child
            os._exit(127)
    return pid, master_fd


def kill_process_group(pid: int) -> None:
    """SIGKILL a PTY shell's whole process group (the shell + anything it runs). The
    group (not the bare pid) matters: pty.fork makes the shell a session leader, so a
    running ``claude``/``vim``/bg job is in its group and would otherwise survive.
    Called on session delete to stop the sandbox's shells before the checkout goes."""
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)  # fallback if getpgid raced the exit


def kill_processes_in_dir(path: str) -> list[int]:
    """SIGKILL every process whose working directory is inside ``path`` — the guaranteed
    hard-kill backstop on session delete/stop. It catches the shell's ``claude`` CLI and
    ANY tool subprocess it started (a build, a script) even while blocked mid-tool,
    because they all run with cwd inside the session's checkout. Run just before the
    checkout is removed (delete) so nothing keeps executing in (or writing to) a deleted
    sandbox, and on stop so a paused session leaves nothing running.

    Linux-only (reads ``/proc/<pid>/cwd``); returns the pids signalled. Each match is
    SIGKILLed INDIVIDUALLY (not via killpg) — a matched process may share this server's
    process group, and killing the group would take the server (or the test runner) down
    with it. Tool children that stayed in the checkout are matched on their own cwd; one
    that cd'd away is left to the cooperative cancel. Never targets this process; an
    unreadable/exited pid is skipped."""
    root = os.path.realpath(path)
    self_pid = os.getpid()
    killed: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return killed
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == self_pid:
            continue
        try:
            cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue  # the process exited or its cwd isn't readable
        if cwd != root and not cwd.startswith(root + os.sep):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed.append(pid)
    return killed


def _close_child(pid: int, fd: int) -> None:
    """Best-effort teardown: kill the shell's process group, reap it, close the master.
    Called when the socket drops or the shell exits, so no PTY/zombie leaks."""
    kill_process_group(pid)
    with contextlib.suppress(OSError):
        os.waitpid(pid, 0)
    with contextlib.suppress(OSError):
        os.close(fd)


async def _write_all(fd: int, data: bytes) -> None:  # pragma: no cover - deploy pump
    """Write ALL of ``data`` to the non-blocking PTY master, yielding when its input
    buffer is full (a large paste exceeds it). A bare ``os.write`` would either
    short-write — silently dropping the tail — or raise BlockingIOError and kill it."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
            view = view[written:]
        except (BlockingIOError, InterruptedError):
            await asyncio.sleep(0)  # let the shell drain the PTY, then retry the rest


async def serve_terminal(  # pragma: no cover - the PTY pump is exercised at deploy
    websocket: WebSocket,
    cwd: str,
    *,
    model: str = "",
    preview_port: int = 0,
    on_open: Callable[[int], None] | None = None,
    on_close: Callable[[int], None] | None = None,
    on_shell_exit: Callable[[int], None] | None = None,
) -> None:
    """Bridge ``websocket`` to a shell PTY in ``cwd`` until either side closes.

    Output is pumped through a queue drained by a single sender task so byte order
    is preserved (vs. a task-per-chunk race). Browser → shell: binary frames are
    written to the PTY verbatim; a text frame is a JSON control (``{"resize":...}``).
    ``model`` (the session's served model) pins the ``claude`` CLI's model tiers so
    it doesn't default to a cloud model the on-box gateway can't serve. ``preview_port``
    (when set) exports ``PORT`` so a ``$PORT``-aware dev server binds the preview port.
    ``on_open`` / ``on_close`` register the PTY pid with the session manager so an open
    shell counts as activity and delete can kill it.

    ``on_shell_exit`` fires ONLY when the shell process itself ended (the PTY child
    EOF'd — the user typed ``exit`` / Ctrl-D), NOT when the browser merely dropped the
    socket (a tab switch or backgrounding). The terminal route wires it to pause the
    session: a deliberate exit shuts the session down, a transient disconnect leaves it
    running.
    """
    from fastapi import WebSocketDisconnect

    overrides: dict[str, str] = {}
    if model:
        overrides.update(model_env(model))
    if preview_port:
        overrides.update(preview_env(preview_port))
    pid, fd = spawn_shell(cwd, env_overrides=overrides or None)
    if on_open is not None:
        on_open(pid)
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    out: asyncio.Queue[bytes | None] = asyncio.Queue()
    # Set when ``os.read`` hits EOF (the shell closed the slave by exiting). The finally
    # uses it to tell a real exit apart from a socket drop before calling on_shell_exit.
    shell_exited = False

    def _on_readable() -> None:
        nonlocal shell_exited
        try:
            data = os.read(fd, _READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:  # the shell exited and closed the slave
            shell_exited = True
            out.put_nowait(None)
            return
        if not data:  # empty read == EOF == the shell exited
            shell_exited = True
        out.put_nowait(data or None)

    loop.add_reader(fd, _on_readable)

    async def _pump_out() -> None:
        while True:
            chunk = await out.get()
            if chunk is None:  # shell EOF — close the socket so the tab settles
                with contextlib.suppress(Exception):
                    await websocket.close()
                return
            await websocket.send_bytes(chunk)

    sender = asyncio.create_task(_pump_out())
    _log.info("terminal open cwd=%s pid=%d", cwd, pid)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                await _write_all(fd, data)
                continue
            text = message.get("text")
            if text:
                with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError):
                    ctl = json.loads(text)
                    if "resize" in ctl:
                        _set_winsize(
                            fd, int(ctl["resize"]["rows"]), int(ctl["resize"]["cols"])
                        )
    except WebSocketDisconnect:
        pass
    finally:
        loop.remove_reader(fd)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        _close_child(pid, fd)
        if on_close is not None:
            on_close(pid)
        # A deliberate shell exit (not a transient socket drop) pauses the session.
        if shell_exited and on_shell_exit is not None:
            on_shell_exit(pid)
        _log.info(
            "terminal close cwd=%s pid=%d shell_exited=%s", cwd, pid, shell_exited
        )
