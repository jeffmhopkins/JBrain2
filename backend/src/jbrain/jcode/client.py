"""The thin httpx client to the jcode control server (and a fake for tests).

Mirrors `SearxngClient`: pinned base URL from config (never model-supplied), a
bearer token, an injectable transport so tests need no network, and a clean
`JcodeError` instead of a leaked stack trace. The control server lives on the
internal `jcode` network; this is the only place the api contacts it.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

# Control calls are quick request/response (no streaming surface); a modest timeout.
_TIMEOUT = httpx.Timeout(30.0)


class JcodeError(RuntimeError):
    """A control-server call failed — surfaced to the route as a clean message."""


class JcodeApi(Protocol):
    """What the api routes depend on — satisfied by the real client and the fake."""

    async def create_session(
        self, repo: str, branch: str, work_branch: str, model: str = ""
    ) -> dict[str, Any]: ...

    async def list_sessions(self) -> list[dict[str, Any]]: ...

    async def get_session(self, sid: str) -> dict[str, Any]: ...

    async def reset(self, sid: str) -> dict[str, Any]: ...

    async def delete(self, sid: str) -> None: ...

    async def stop(self, sid: str) -> dict[str, Any]: ...

    async def restart(self, sid: str) -> dict[str, Any]: ...

    async def preview_status(self, sid: str) -> dict[str, Any]: ...

    async def preview_open(self, sid: str, port: int | None) -> dict[str, Any]: ...

    async def preview_close(self, sid: str) -> None: ...


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

    async def create_session(
        self, repo: str, branch: str, work_branch: str, model: str = ""
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/sessions",
            json={"repo": repo, "branch": branch, "work_branch": work_branch, "model": model},
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/sessions")

    async def get_session(self, sid: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{sid}")

    async def reset(self, sid: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/reset")

    async def delete(self, sid: str) -> None:
        # Idempotent: a 404 means the control server already dropped this session
        # (idle reaper, or a restart that lost its in-memory index), which is the
        # delete's desired end state. Swallow it so the route still removes the
        # durable launcher row instead of 502-ing and stranding it forever.
        try:
            async with self._client() as client:
                resp = await client.request("DELETE", f"/sessions/{sid}")
                if resp.status_code != httpx.codes.NOT_FOUND:
                    resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JcodeError(f"jcode control server error: {exc}") from exc

    async def stop(self, sid: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/stop")

    async def restart(self, sid: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/restart")

    async def preview_status(self, sid: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{sid}/preview")

    async def preview_open(self, sid: str, port: int | None) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/preview", json={"port": port})

    async def preview_close(self, sid: str) -> None:
        await self._json("DELETE", f"/sessions/{sid}/preview")


class FakeJcodeClient:
    """In-memory control server for tests: no httpx, no network."""

    def __init__(self, *, preview_enabled: bool = True) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._n = 0
        # The model passed to each create_session, in order — tests assert the api
        # resolved + forwarded the owner's selection.
        self.created_models: list[str] = []
        self._preview_enabled = preview_enabled
        self._previews: dict[str, str] = {}

    async def create_session(
        self, repo: str, branch: str, work_branch: str, model: str = ""
    ) -> dict[str, Any]:
        self._n += 1
        sid = f"sess{self._n}"
        self.created_models.append(model)
        s = {
            "id": sid,
            "repo": repo,
            "branch": branch,
            "work_branch": work_branch or f"jcode/{sid}",
            "model": model,
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

    async def stop(self, sid: str) -> dict[str, Any]:
        s = await self.get_session(sid)
        s["status"] = "stopped"
        return s

    async def restart(self, sid: str) -> dict[str, Any]:
        s = await self.get_session(sid)
        s["status"] = "ready"
        return s

    async def preview_status(self, sid: str) -> dict[str, Any]:
        return {"enabled": self._preview_enabled, "url": self._previews.get(sid)}

    async def preview_open(self, sid: str, port: int | None) -> dict[str, Any]:
        if not self._preview_enabled:
            raise JcodeError("web preview is not enabled")
        url = f"https://demo-{sid}.trycloudflare.com"
        self._previews[sid] = url
        return {"enabled": True, "url": url}

    async def preview_close(self, sid: str) -> None:
        self._previews.pop(sid, None)
