"""The thin httpx client to the jcode control server (and a fake for tests).

Mirrors `SearxngClient`: pinned base URL from config (never model-supplied), a
bearer token, an injectable transport so tests need no network, and a clean
`JcodeError` instead of a leaked stack trace. The control server lives on the
internal `jcode` network; this is the only place the api contacts it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

# A turn streams for a while; cap connect/write but let the read run (SSE frames
# arrive sporadically — a long blocking tool may be silent for seconds).
_TIMEOUT = httpx.Timeout(30.0, read=None)


class JcodeError(RuntimeError):
    """A control-server call failed — surfaced to the route as a clean message."""


class JcodeApi(Protocol):
    """What the api routes depend on — satisfied by the real client and the fake."""

    async def create_session(self, repo: str, branch: str, work_branch: str) -> dict[str, Any]: ...

    async def list_sessions(self) -> list[dict[str, Any]]: ...

    async def get_session(self, sid: str) -> dict[str, Any]: ...

    async def reset(self, sid: str) -> dict[str, Any]: ...

    async def delete(self, sid: str) -> None: ...

    async def cancel(self, sid: str) -> None: ...

    def stream_turn(self, sid: str, prompt: str) -> AsyncIterator[bytes]: ...


class JcodeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        if not self._base_url:
            raise JcodeError("code mode is not configured")
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=_TIMEOUT,
            transport=self._transport,
        )

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        try:
            async with self._client() as client:
                resp = await client.request(method, path, **kw)
                resp.raise_for_status()
                return resp.json() if resp.content else None
        except httpx.HTTPError as exc:
            raise JcodeError(f"jcode control server error: {exc}") from exc

    async def create_session(self, repo: str, branch: str, work_branch: str) -> dict[str, Any]:
        return await self._json(
            "POST", "/sessions", json={"repo": repo, "branch": branch, "work_branch": work_branch}
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/sessions")

    async def get_session(self, sid: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{sid}")

    async def reset(self, sid: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/reset")

    async def delete(self, sid: str) -> None:
        await self._json("DELETE", f"/sessions/{sid}")

    async def cancel(self, sid: str) -> None:
        await self._json("POST", f"/sessions/{sid}/cancel")

    async def stream_turn(self, sid: str, prompt: str) -> AsyncIterator[bytes]:
        """Proxy the control server's SSE, yielding one complete `data:` frame per
        event (so the caller's frame buffer / reconnect offset counts real events)."""
        try:
            async with (
                self._client() as client,
                client.stream("POST", f"/sessions/{sid}/turn", json={"prompt": prompt}) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        yield f"{line}\n\n".encode()
        except httpx.HTTPError as exc:
            raise JcodeError(f"jcode turn error: {exc}") from exc


class FakeJcodeClient:
    """In-memory control server for tests: no httpx, no network."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._n = 0
        self.cancelled: list[str] = []

    async def create_session(self, repo: str, branch: str, work_branch: str) -> dict[str, Any]:
        self._n += 1
        sid = f"sess{self._n}"
        s = {
            "id": sid,
            "repo": repo,
            "branch": branch,
            "work_branch": work_branch or f"jcode/{sid}",
            "status": "ready",
            "created_at": "2026-06-25T00:00:00+00:00",
            "last_active_at": "2026-06-25T00:00:00+00:00",
        }
        self._sessions[sid] = s
        return s

    async def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions.values())

    async def get_session(self, sid: str) -> dict[str, Any]:
        if sid not in self._sessions:
            raise JcodeError(f"unknown session: {sid}")
        return self._sessions[sid]

    async def reset(self, sid: str) -> dict[str, Any]:
        return await self.get_session(sid)

    async def delete(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    async def cancel(self, sid: str) -> None:
        self.cancelled.append(sid)

    async def stream_turn(self, sid: str, prompt: str) -> AsyncIterator[bytes]:
        for payload in (
            '{"type": "text", "text": "On it.", "tool": "", "data": {}}',
            '{"type": "tool_use", "text": "", "tool": "Edit", "data": {}}',
            '{"type": "done", "text": "", "tool": "", "data": {}}',
        ):
            yield f"data: {payload}\n\n".encode()
