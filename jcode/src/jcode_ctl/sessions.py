"""Session manager: the sandboxed coding sessions and their lifecycle.

Holds no owner data — a session is an isolated git checkout plus the metadata the
launcher needs (the api mirrors that metadata into its owner-only
``jcode_sessions`` table in Wave J2). Time is injected so tests are deterministic.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from jcode_ctl.agent import CodingAgent, TurnEvent
from jcode_ctl.workspace import Workspace

Status = Literal["ready", "running", "error"]


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
    # The served-model id the agent runs for this session (empty = the agent's
    # configured default). Fixed at create so a turn never swaps model mid-session.
    model: str = ""

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
        now: Callable[[], datetime] = _utcnow,
        new_id: Callable[[], str] = lambda: secrets.token_hex(4),
    ) -> None:
        self._agent = agent
        self._workspace = workspace
        self._root = Path(workspace_root)
        self._max = max_sessions
        self._now = now
        self._new_id = new_id
        self._sessions: dict[str, Session] = {}

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
        session.status = "running"
        session.last_active_at = self._stamp()
        try:
            async for ev in self._agent.run_turn(
                sid, prompt, session.workspace, model=session.model
            ):
                if ev.type == "error":
                    session.status = "error"
                yield ev
        finally:
            if session.status == "running":
                session.status = "ready"
            session.last_active_at = self._stamp()

    async def cancel(self, sid: str) -> None:
        self.get(sid)
        await self._agent.cancel(sid)

    async def reset(self, sid: str) -> Session:
        session = self.get(sid)
        await self._workspace.reset(Path(session.workspace))
        session.status = "ready"
        session.last_active_at = self._stamp()
        return session

    def delete(self, sid: str) -> None:
        session = self.get(sid)
        self._workspace.remove(Path(session.workspace))
        del self._sessions[sid]
        # Drop the agent's per-session state (resume id, cancel flag) so it can't
        # outlive the session.
        self._agent.forget(sid)

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
            and datetime.fromisoformat(s.last_active_at) < cutoff
        ]
