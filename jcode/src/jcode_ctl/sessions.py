"""Session manager: the sandboxed coding sessions and their lifecycle.

Holds no owner data — a session is an isolated git checkout plus the metadata the
launcher needs (the api mirrors that metadata into its owner-only
``jcode_sessions`` table in Wave J2). Time is injected so tests are deterministic.

The session is driven entirely through its interactive terminal (a real shell in the
checkout, ``terminal.py``); there is no headless agent. Exiting the shell pauses the
session (``stop``) while keeping the checkout on disk, so it can be ``restart``ed later
from the launcher.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from jcode_ctl.terminal import kill_process_group, kill_processes_in_dir
from jcode_ctl.workspace import Workspace

# "stopped" is a deliberately-paused session: its processes are killed but its checkout
# is kept, so a restart picks up where it left off. The reaper never touches it.
Status = Literal["ready", "stopped"]

_log = logging.getLogger("jcode_ctl.sessions")


class SessionError(RuntimeError):
    """A session operation failed (unknown id, at capacity, …)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Session:
    id: str
    repo: str
    branch: str
    work_branch: str
    workspace: str
    status: Status
    created_at: str
    last_active_at: str
    # The served-model id the session's terminal pins the ``claude`` CLI to (empty =
    # the server's configured default). Fixed at create.
    model: str = ""

    def public(self) -> dict[str, object]:
        return asdict(self)


class SessionManager:
    def __init__(
        self,
        workspace: Workspace,
        workspace_root: str,
        *,
        home_root: str = "/work/.home",
        max_sessions: int = 8,
        now: Callable[[], datetime] = _utcnow,
        new_id: Callable[[], str] = lambda: secrets.token_hex(4),
    ) -> None:
        self._workspace = workspace
        self._root = Path(workspace_root)
        self._home_root = Path(home_root)
        self._max = max_sessions
        self._now = now
        self._new_id = new_id
        self._sessions: dict[str, Session] = {}
        # sid -> the pids of its open interactive-terminal PTYs (the shell session
        # leaders). A live terminal is activity (the reaper must never remove a checkout
        # out from under an open shell), and on delete/stop these groups are killed so
        # the shell + anything it's running stop before the checkout is torn down.
        self._terminals: dict[str, set[int]] = {}

    def _stamp(self) -> str:
        return self._now().isoformat()

    def home_for(self, sid: str) -> Path:
        """The session's private $HOME dir. Derived (not stored on the session) so the
        api's mirrored session shape is untouched; the terminal sets HOME to this and a
        per-session bin under it leads PATH (see JCODE_SESSION_TOOLS_PLAN)."""
        return self._home_root / sid

    async def create(
        self, repo: str, branch: str = "main", work_branch: str = "", *, model: str = ""
    ) -> Session:
        if self._max > 0 and len(self._sessions) >= self._max:
            raise SessionError(f"at capacity ({self._max} sessions) — close one first")
        sid = self._new_id()
        work_branch = work_branch or f"jcode/{sid}"
        path = self._root / sid
        await self._workspace.clone(path, repo, branch, work_branch)
        # The session's private $HOME (its own tool bin on PATH, ~/.grok, npm prefix).
        self._workspace.prepare_home(self.home_for(sid))
        now = self._stamp()
        session = Session(
            id=sid,
            repo=repo,
            branch=branch,
            work_branch=work_branch,
            workspace=str(path),
            status="ready",
            created_at=now,
            last_active_at=now,
            model=model,
        )
        self._sessions[sid] = session
        _log.info(
            "session create sid=%s repo=%s branch=%s work_branch=%s model=%s",
            sid,
            repo,
            branch,
            work_branch,
            model or "<default>",
        )
        return session

    def list(self) -> list[Session]:
        return sorted(
            self._sessions.values(), key=lambda s: s.last_active_at, reverse=True
        )

    def get(self, sid: str) -> Session:
        try:
            return self._sessions[sid]
        except KeyError as exc:
            raise SessionError(f"unknown session: {sid}") from exc

    def get_or_none(self, sid: str) -> Session | None:
        """Like ``get`` but returns None for an unknown id — lets the reaper re-check a
        session right before deleting it without racing on a SessionError."""
        return self._sessions.get(sid)

    def terminal_opened(self, sid: str, pid: int) -> None:
        """Register an interactive terminal's PTY pid for a session (the WS route).
        Tracking the pid lets concurrent terminals nest, keeps the session fresh against
        the reaper, AND lets stop/delete kill the shell's process group. Opening a
        terminal on a paused session brings it back to ``ready``."""
        session = self.get(sid)
        self._terminals.setdefault(sid, set()).add(pid)
        session.status = "ready"
        session.last_active_at = self._stamp()
        _log.debug("terminal opened sid=%s pid=%d", sid, pid)

    def terminal_closed(self, sid: str, pid: int) -> None:
        """Drop a closed terminal's pid; remove the session's entry once none remain so
        ``idle_sessions`` can reap it again."""
        pids = self._terminals.get(sid)
        if pids is not None:
            pids.discard(pid)
            if not pids:
                self._terminals.pop(sid, None)
        if sid in self._sessions:
            self._sessions[sid].last_active_at = self._stamp()
        _log.debug("terminal closed sid=%s pid=%d", sid, pid)

    def stop(self, sid: str) -> Session:
        """Pause a session: SIGKILL its open terminals' process groups and anything
        still running in the checkout, then mark it ``stopped`` — but KEEP the checkout
        on disk so a restart resumes the same work. Fired when the shell exits (Ctrl-D /
        ``exit``) and reachable explicitly from the launcher. Idempotent."""
        session = self.get(sid)
        for pid in self._terminals.pop(sid, set()):
            kill_process_group(pid)
        # Backstop: SIGKILL any tool subprocess still running in the checkout (a build,
        # a bg job) so a paused session leaves nothing executing.
        with contextlib.suppress(Exception):
            kill_processes_in_dir(session.workspace)
        session.status = "stopped"
        session.last_active_at = self._stamp()
        _log.info("session stop sid=%s", sid)
        return session

    def restart(self, sid: str) -> Session:
        """Resume a paused session — the checkout is still on disk, so this just flips
        it back to ``ready`` and refreshes activity. A terminal can then reattach."""
        session = self.get(sid)
        session.status = "ready"
        session.last_active_at = self._stamp()
        _log.info("session restart sid=%s", sid)
        return session

    async def reset(self, sid: str) -> Session:
        session = self.get(sid)
        await self._workspace.reset(Path(session.workspace))
        session.status = "ready"
        session.last_active_at = self._stamp()
        _log.info("session reset sid=%s", sid)
        return session

    async def delete(self, sid: str) -> None:
        session = self.get(sid)
        # Stop everything running in the sandbox BEFORE the checkout is pulled out from
        # under it: SIGKILL each open terminal's process group (the shell + whatever
        # it's running), then a hard backstop over the checkout dir.
        for pid in self._terminals.pop(sid, set()):
            kill_process_group(pid)
        kill_processes_in_dir(session.workspace)
        self._workspace.remove(Path(session.workspace))
        # Purge the session's private $HOME alongside the checkout (its installed tools,
        # ~/.grok, npm cache) so a deleted session leaves nothing on the volume.
        self._workspace.remove(self.home_for(sid))
        del self._sessions[sid]
        _log.info("session delete sid=%s", sid)

    def idle_sessions(
        self, *, ttl_seconds: int, now: datetime | None = None
    ) -> list[str]:
        """Ids of sessions with no activity for ``ttl_seconds`` (0 disables). A session
        with an open terminal keeps fresh, and a ``stopped`` (deliberately paused)
        session is never reaped — its checkout is kept for a restart."""
        if ttl_seconds <= 0:
            return []
        # Stamps are always tz-aware UTC (``_utcnow``/the injected clock), so this
        # compares against a tz-aware cutoff without raising on naive/aware mismatch.
        cutoff = (now or self._now()) - timedelta(seconds=ttl_seconds)
        return [
            s.id
            for s in self._sessions.values()
            if s.status != "stopped"
            and s.id not in self._terminals
            and datetime.fromisoformat(s.last_active_at) < cutoff
        ]
