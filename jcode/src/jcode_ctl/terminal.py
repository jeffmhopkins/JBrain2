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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import WebSocket

_log = logging.getLogger("jcode_ctl.terminal")

# A login bash so the sandbox's profile (PATH, the agent's env) is in effect.
_SHELL: tuple[str, ...] = ("/bin/bash", "-l")
_READ_BYTES = 65536


def model_env(model: str, planner: str = "") -> dict[str, str]:
    """Env that pins every model tier the interactive coding CLIs might pick to the
    session's on-box model. Without it ``claude`` defaults to a cloud model
    (``claude-opus-4-…``) the local gateway has no route for, and every session errors
    "the selected model may not exist". ``claude`` resolves its ``/model`` aliases
    (opus/sonnet/haiku/fable) and its background summariser through the ANTHROPIC_*
    vars, so on a single-model gateway they must ALL map to the one served route;
    ``GROK_MODEL`` does the same for the Grok CLI (``grok``). This pins the model for
    the shell's CLIs so the per-session quant the owner picked is what each requests.

    ``planner`` is the served-model for grok's ``plan`` subagent (``[subagents.models]
    plan``); it is exported as ``JCODE_GROK_PLAN_MODEL`` — ALWAYS, even when empty, so a
    single-model session (planner == executor) explicitly clears any image-level default
    and ``grok-config.sh`` omits the plan pin instead of inheriting one."""
    return {
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
        "GROK_MODEL": model,
        "JCODE_GROK_PLAN_MODEL": planner,
    }


def home_env(home: str) -> dict[str, str]:
    """Give the session its OWN ``$HOME`` and a private, PATH-leading bin dir.

    A per-session HOME gives per-session ``~/.grok`` (via ``grok-config.sh``),
    ``~/.claude``, and shell history; ``NPM_CONFIG_PREFIX`` sends a per-session
    ``npm i -g`` to ``$HOME/.npm-global`` too. A binary in ``$HOME/.local/bin``
    shadows the image's ``/usr/local/bin`` copy for THIS session only — that
    shadowing IS the per-session tool-version mechanism (see JCODE_SESSION_TOOLS_PLAN).

    The PATH set here is the base for non-login execs; the interactive ``bash -l``
    re-prepends these dirs via ``/etc/profile.d/jcode-path.sh``, because Debian's
    ``/etc/profile`` resets root's PATH and would otherwise drop them. ``$HOME`` and
    the two markers below survive that reset, which is what the snippet reads."""
    tools_bin = f"{home}/.local/bin"
    npm_bin = f"{home}/.npm-global/bin"
    base = os.environ.get("PATH", "")
    path = f"{tools_bin}:{npm_bin}:{base}" if base else f"{tools_bin}:{npm_bin}"
    return {
        "HOME": home,
        "PATH": path,
        "JCODE_TOOLS_BIN": tools_bin,
        "NPM_CONFIG_PREFIX": f"{home}/.npm-global",
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


# Recent raw PTY output retained per terminal so a reconnecting client can be replayed
# the current screen (it lands on a fresh xterm with no history). Bounded — a couple of
# screens of scrollback restores context without unbounded growth while away.
_SCROLLBACK_BYTES = 256 * 1024


class PtyTerminal:
    """A persistent shell PTY bound to a jcode session, decoupled from any one socket.

    The shell keeps running while no client is attached — you leave the app and your
    build / ``claude`` run keeps going — and a reconnecting client reattaches and is
    replayed the recent scrollback so it sees the current screen. A loop reader drains
    the PTY continuously (independent of any socket) so output is never lost and the
    shell never blocks on a full PTY buffer while detached. Torn down only when the
    shell itself exits (EOF) or an external kill (stop / delete / shutdown) ends it.
    """

    def __init__(
        self,
        cwd: str,
        *,
        env_overrides: dict[str, str] | None = None,
        scrollback: int = _SCROLLBACK_BYTES,
    ) -> None:
        self.pid, self.fd = spawn_shell(cwd, env_overrides=env_overrides)
        os.set_blocking(self.fd, False)
        self._loop = asyncio.get_running_loop()
        self._limit = scrollback
        self._buf = bytearray()  # retained tail of raw output, for replay on reattach
        self._produced = 0  # total bytes ever read from the PTY (absolute offset base)
        self._exited = False  # the shell EOF'd, or we tore it down
        self._closed = False  # teardown ran (guards it to once)
        self._new = asyncio.Event()  # poked on new output / exit so stream_to wakes
        # The client socket currently driving, or None when detached (takeover state).
        # Typed loosely (the WS shape varies: Starlette in prod, a fake in tests).
        self.attached: Any = None
        # Fired once when the SHELL exits on its own (Ctrl-D / ``exit``) — NOT on an
        # external kill. The route wires it to deregister the pid + pause the session.
        self.on_exit: Callable[[], None] | None = None
        self._loop.add_reader(self.fd, self._on_readable)

    @property
    def alive(self) -> bool:
        return not self._closed

    @property
    def _origin(self) -> int:
        # Absolute offset of the first retained byte (earlier bytes were trimmed off).
        return self._produced - len(self._buf)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.fd, _READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:  # the shell exited and closed the slave
            data = b""
        if not data:  # empty read == EOF == the shell exited
            self._teardown(shell_exit=True)
            return
        self._buf += data
        self._produced += len(data)
        if len(self._buf) > self._limit:  # keep only the tail
            del self._buf[: len(self._buf) - self._limit]
        self._new.set()

    async def write(self, data: bytes) -> None:
        if not self._closed:
            await _write_all(self.fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if not self._closed:
            with contextlib.suppress(OSError):
                _set_winsize(self.fd, rows, cols)

    async def stream_to(self, websocket: Any) -> bool:
        """Replay the retained scrollback, then forward live output to ``websocket``
        until the shell exits or this coroutine is cancelled (the client detached).
        Returns True if it stopped because the shell exited, so the caller closes it."""
        sent = self._origin  # start by replaying everything we still retain
        while True:
            while sent < self._produced:
                origin = self._origin
                sent = max(sent, origin)  # fell behind the ring? resync past the gap
                chunk = bytes(self._buf[sent - origin :])
                sent = self._produced
                await websocket.send_bytes(chunk)
            if self._exited:
                return True
            self._new.clear()
            if sent < self._produced or self._exited:
                continue  # raced new output / exit between the check and the clear
            await self._new.wait()

    def close(self) -> None:
        """External teardown (server shutdown): kill the shell, do NOT fire on_exit."""
        self._teardown(shell_exit=False)

    def _teardown(self, *, shell_exit: bool) -> None:
        if self._closed:
            return
        self._closed = True
        self._exited = True
        with contextlib.suppress(ValueError, OSError):
            self._loop.remove_reader(self.fd)
        _close_child(self.pid, self.fd)  # kill the group, reap, close the master fd
        self._new.set()  # wake any stream_to so it returns
        if shell_exit and self.on_exit is not None:
            self.on_exit()


class TerminalRegistry:
    """The live persistent terminals, keyed by session id. One shell per session; a
    reconnect reattaches to the existing one. Stale (exited) entries are forgotten."""

    def __init__(self) -> None:
        self._terms: dict[str, PtyTerminal] = {}

    def get(self, sid: str) -> PtyTerminal | None:
        term = self._terms.get(sid)
        if term is not None and not term.alive:  # exited but not yet swept — forget it
            self._terms.pop(sid, None)
            return None
        return term

    def get_or_create(
        self, sid: str, cwd: str, *, env_overrides: dict[str, str] | None
    ) -> tuple[PtyTerminal, bool]:
        term = self.get(sid)
        if term is not None:
            return term, False
        term = PtyTerminal(cwd, env_overrides=env_overrides)
        self._terms[sid] = term
        return term, True

    def remove(self, sid: str, term: PtyTerminal | None = None) -> None:
        current = self._terms.get(sid)
        if current is not None and (term is None or current is term):
            self._terms.pop(sid, None)

    def close_all(self) -> None:
        for term in list(self._terms.values()):
            term.close()
        self._terms.clear()


async def _recv_loop(websocket: WebSocket, term: PtyTerminal) -> None:
    """Browser → shell until the socket drops: binary frames to the PTY verbatim, a text
    frame is a JSON resize control. Returns on disconnect (the client detached)."""
    from fastapi import WebSocketDisconnect

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                await term.write(data)
                continue
            text = message.get("text")
            if text:
                with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError):
                    ctl = json.loads(text)
                    if "resize" in ctl:
                        term.resize(
                            int(ctl["resize"]["rows"]), int(ctl["resize"]["cols"])
                        )
    except WebSocketDisconnect:
        return


async def serve_terminal(
    websocket: WebSocket,
    sid: str,
    registry: TerminalRegistry,
    cwd: str,
    *,
    model: str = "",
    planner: str = "",
    preview_port: int = 0,
    home: str = "",
    on_open: Callable[[int], None] | None = None,
    on_close: Callable[[int], None] | None = None,
    on_shell_exit: Callable[[int], None] | None = None,
) -> None:
    """Attach ``websocket`` to the session's persistent shell PTY, creating it on the
    first connect and reattaching (with a scrollback replay) on later ones.

    The shell outlives the socket: a browser disconnect (tab close, leaving the app)
    detaches but leaves the PTY running, so work in progress keeps going and the session
    is NOT paused. A second connect takes over — the previous socket is closed so a
    single client drives at a time. ``model`` pins the ``claude`` CLI's model tiers (no
    cloud default the on-box gateway can't serve); ``preview_port`` exports ``PORT`` so
    a ``$PORT``-aware dev server binds the preview port. ``on_open`` registers the pid
    the first time the shell is created (an open shell counts as activity and
    stop/delete can kill it); ``on_close`` / ``on_shell_exit`` fire only when the SHELL
    exits (EOF — the user typed ``exit`` / Ctrl-D), pausing it. An external kill
    (stop / delete) ends the shell without firing them — that path already removed it.
    """
    overrides: dict[str, str] = {}
    if home:
        overrides.update(home_env(home))
    if model:
        overrides.update(model_env(model, planner))
    if preview_port:
        overrides.update(preview_env(preview_port))

    term, created = registry.get_or_create(sid, cwd, env_overrides=overrides or None)
    if created:
        if on_open is not None:
            on_open(term.pid)

        def _on_exit() -> None:
            registry.remove(sid, term)
            if on_close is not None:
                on_close(term.pid)
            if on_shell_exit is not None:
                on_shell_exit(term.pid)

        term.on_exit = _on_exit
        _log.info("terminal open cwd=%s pid=%d", cwd, term.pid)
    else:
        _log.info("terminal reattach cwd=%s pid=%d", cwd, term.pid)

    # Takeover: a new client closes whatever socket was attached, so one drives at once.
    # Claim the slot BEFORE closing the old socket, so the departing attach's detach
    # guard (``term.attached is websocket``) sees the new owner and can't clear it.
    previous = term.attached
    term.attached = websocket
    if previous is not None and previous is not websocket:
        with contextlib.suppress(Exception):
            await previous.close()

    recv = asyncio.ensure_future(_recv_loop(websocket, term))
    send = asyncio.ensure_future(term.stream_to(websocket))
    try:
        done, pending = await asyncio.wait(
            {recv, send}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # The shell exited (stream_to returned True) → close the tab so it settles. A
        # send that errored (a broken socket) just ends the attach like a disconnect.
        if send in done and send.exception() is None and send.result():
            with contextlib.suppress(Exception):
                await websocket.close()
    finally:
        # Detach without killing — unless another client already took over this slot.
        if term.attached is websocket:
            term.attached = None
        _log.info(
            "terminal detach cwd=%s pid=%d exited=%s", cwd, term.pid, not term.alive
        )
