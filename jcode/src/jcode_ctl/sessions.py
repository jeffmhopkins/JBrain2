"""Session manager: the sandboxed coding sessions and their lifecycle.

Holds no owner data — a session is an isolated git checkout plus the metadata the
launcher needs (the api mirrors that metadata into its owner-only
``jcode_sessions`` table in Wave J2). Time is injected so tests are deterministic.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from jcode_ctl.agent import CodingAgent, TurnEvent
from jcode_ctl.workspace import Workspace

Status = str  # "ready" | "running" | "error"


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
        self, repo: str, branch: str = "main", work_branch: str = ""
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

    async def run_turn(self, sid: str, prompt: str) -> AsyncIterator[TurnEvent]:
        session = self.get(sid)
        session.status = "running"
        session.last_active_at = self._stamp()
        try:
            async for ev in self._agent.run_turn(sid, prompt, session.workspace):
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
