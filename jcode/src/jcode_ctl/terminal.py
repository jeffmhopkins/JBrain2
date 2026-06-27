"""Interactive PTY terminal: a real shell in a session's checkout, over a WebSocket.

The control server forks a login shell under a pseudo-terminal rooted in the
session's workspace and bridges it to the WebSocket — keystrokes/paste in, raw
terminal bytes out, plus a resize control message. The api proxies this to the
owner's browser (xterm.js); this server stays internal + token-authed.

Safe by construction the same way the headless agent is: the sandbox is an
isolated, throwaway per-session checkout on its own network (no host, no notes,
no other services), so a shell in it can do no more than the agent already can
with ``bypassPermissions``. The child inherits the process env (so ``claude``,
git, etc. resolve the same gateway the agent uses) plus a real ``TERM``.
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
    single-model gateway they must ALL map to the one served route. The headless agent
    passes the model explicitly; this is the interactive shell's equivalent."""
    return {
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
    }


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


def _close_child(pid: int, fd: int) -> None:
    """Best-effort teardown: kill the shell's whole process group and reap it, close the
    master. Called when the socket drops or the shell exits, so no PTY/zombie leaks. The
    group (not the bare pid) matters: pty.fork makes the shell a session leader, so a
    running ``claude``/``vim``/bg job is in its group and would otherwise orphan."""
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)  # fallback if getpgid raced the exit
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
    websocket: WebSocket, cwd: str, *, model: str = ""
) -> None:
    """Bridge ``websocket`` to a shell PTY in ``cwd`` until either side closes.

    Output is pumped through a queue drained by a single sender task so byte order
    is preserved (vs. a task-per-chunk race). Browser → shell: binary frames are
    written to the PTY verbatim; a text frame is a JSON control (``{"resize":...}``).
    ``model`` (the session's served model) pins the ``claude`` CLI's model tiers so
    it doesn't default to a cloud model the on-box gateway can't serve.
    """
    from fastapi import WebSocketDisconnect

    pid, fd = spawn_shell(cwd, env_overrides=model_env(model) if model else None)
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    out: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(fd, _READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:  # the shell exited and closed the slave
            out.put_nowait(None)
            return
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
        _log.info("terminal close cwd=%s pid=%d", cwd, pid)
