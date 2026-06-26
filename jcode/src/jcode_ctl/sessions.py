"""Session manager: the sandboxed coding sessions and their lifecycle.

Holds no owner data — a session is an isolated git checkout plus the metadata the
launcher needs (the api mirrors that metadata into its owner-only
``jcode_sessions`` table in Wave J2). Time is injected so tests are deterministic.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from jcode_ctl.agent import CodingAgent, TurnEvent
from jcode_ctl.workspace import Workspace

Status = Literal["ready", "running", "error"]

_BYTES_PER_MB = 1024 * 1024

_log = logging.getLogger("jcode_ctl.sessions")


class SessionError(RuntimeError):
    """A session operation failed (unknown id, at capacity, …)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def directory_size_mb(path: Path) -> int:
    """Total size of the regular files under ``path``, in whole MB. Symlinks are counted
    as their own (tiny) entry and never followed — a link can't inflate the checkout's
    measured size or let it escape the session dir. A missing path is 0 (a not-yet-
    cloned or already-removed checkout doesn't read as over quota)."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total // _BYTES_PER_MB


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
    # The served-model id the agent runs for this session (empty = the agent's
    # configured default). Fixed at create so a turn never swaps model mid-session.
    model: str = ""
    # True once the checkout exceeded the disk ceiling and turns are refused until it's
    # reset/deleted. Surfaced to the api/UI so the owner sees WHY a turn won't start.
    over_quota: bool = False

    def public(self) -> dict[str, object]:
        return asdict(self)


class SessionManager:
    def __init__(
        self,
        agent: CodingAgent,
        workspace: Workspace,
        workspace_root: str,
        *,
        max_sessions: int = 8,
        max_concurrent_turns: int = 0,
        session_disk_limit_mb: int = 0,
        now: Callable[[], datetime] = _utcnow,
        new_id: Callable[[], str] = lambda: secrets.token_hex(4),
    ) -> None:
        self._agent = agent
        self._workspace = workspace
        self._root = Path(workspace_root)
        self._max = max_sessions
        self._max_turns = max_concurrent_turns
        self._disk_limit_mb = session_disk_limit_mb
        self._now = now
        self._new_id = new_id
        self._sessions: dict[str, Session] = {}
        # Count of turns currently streaming, across all sessions — gated by
        # _max_turns so a burst can't thrash the aggregate CPU/mem caps.
        self._active_turns = 0
        # sid -> count of open interactive terminals. A live terminal is activity:
        # the reaper must never remove a checkout out from under an open shell.
        self._terminals: dict[str, int] = {}

    def _stamp(self) -> str:
        return self._now().isoformat()

    async def create(
        self, repo: str, branch: str = "main", work_branch: str = "", *, model: str = ""
    ) -> Session:
        if self._max > 0 and len(self._sessions) >= self._max:
            raise SessionError(f"at capacity ({self._max} sessions) — close one first")
        sid = self._new_id()
        work_branch = work_branch or f"jcode/{sid}"
        path = self._root / sid
        await self._workspace.clone(path, repo, branch, work_branch)
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

    async def run_turn(self, sid: str, prompt: str) -> AsyncIterator[TurnEvent]:
        session = self.get(sid)
        # Refuse BEFORE marking running / incrementing the counter, so a rejected turn
        # leaves the session untouched. Capacity first (cheap), then the disk sweep.
        if self._max_turns > 0 and self._active_turns >= self._max_turns:
            raise SessionError(
                f"at turn capacity ({self._max_turns} turns running) — "
                "wait for one to finish"
            )
        if self._disk_limit_mb > 0:
            used_mb = directory_size_mb(Path(session.workspace))
            session.over_quota = used_mb > self._disk_limit_mb
            if session.over_quota:
                raise SessionError(
                    f"session over disk quota ({used_mb} MB > "
                    f"{self._disk_limit_mb} MB) — reset or delete it to free space"
                )
        session.status = "running"
        session.last_active_at = self._stamp()
        self._active_turns += 1
        _log.info(
            "turn start sid=%s model=%s prompt_chars=%d active_turns=%d",
            sid,
            session.model or "<default>",
            len(prompt),
            self._active_turns,
        )
        events = 0
        try:
            async for ev in self._agent.run_turn(
                sid, prompt, session.workspace, model=session.model
            ):
                events += 1
                if ev.type == "error":
                    session.status = "error"
                    # A user-initiated cancel surfaces as an error event but isn't a
                    # failure — log it at INFO so real errors stand out in the log.
                    if ev.text == "cancelled":
                        _log.info("turn cancelled sid=%s", sid)
                    else:
                        _log.error("turn error sid=%s: %s", sid, ev.text)
                yield ev
        finally:
            self._active_turns -= 1
            if session.status == "running":
                session.status = "ready"
            session.last_active_at = self._stamp()
            _log.info(
                "turn end sid=%s status=%s events=%d", sid, session.status, events
            )

    def terminal_opened(self, sid: str) -> None:
        """Note an interactive terminal opening on a session (the WS route). Counts so
        concurrent terminals nest, and stamps activity so an open shell stays fresh."""
        self.get(sid)
        self._terminals[sid] = self._terminals.get(sid, 0) + 1
        self._sessions[sid].last_active_at = self._stamp()

    def terminal_closed(self, sid: str) -> None:
        """Note an interactive terminal closing. Drops the session's entry at zero so
        ``idle_sessions`` can reap it again once no terminal is open."""
        remaining = self._terminals.get(sid, 0) - 1
        if remaining <= 0:
            self._terminals.pop(sid, None)
        else:
            self._terminals[sid] = remaining
        if sid in self._sessions:
            self._sessions[sid].last_active_at = self._stamp()

    async def cancel(self, sid: str) -> None:
        self.get(sid)
        await self._agent.cancel(sid)

    async def reset(self, sid: str) -> Session:
        session = self.get(sid)
        await self._workspace.reset(Path(session.workspace))
        session.status = "ready"
        # A hard reset/clean drops the bloat that tripped the ceiling, so clear the
        # flag — the next turn re-measures and re-trips it only if it's still over.
        session.over_quota = False
        session.last_active_at = self._stamp()
        return session

    def delete(self, sid: str) -> None:
        session = self.get(sid)
        self._workspace.remove(Path(session.workspace))
        del self._sessions[sid]
        # Drop the agent's per-session state (resume id, cancel flag) so it can't
        # outlive the session.
        self._agent.forget(sid)
        _log.info("session delete sid=%s", sid)

    def idle_sessions(
        self, *, ttl_seconds: int, now: datetime | None = None
    ) -> list[str]:
        """Ids of sessions with no activity for ``ttl_seconds`` (0 disables). A running
        turn keeps a session fresh, so an in-flight session is never reaped."""
        if ttl_seconds <= 0:
            return []
        # Stamps are always tz-aware UTC (``_utcnow``/the injected clock), so this
        # compares against a tz-aware cutoff without raising on naive/aware mismatch.
        cutoff = (now or self._now()) - timedelta(seconds=ttl_seconds)
        return [
            s.id
            for s in self._sessions.values()
            if s.status != "running"
            and s.id not in self._terminals
            and datetime.fromisoformat(s.last_active_at) < cutoff
        ]
