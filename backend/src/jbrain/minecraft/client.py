"""The api's one way to the Minecraft sidecar and to its container's lifecycle.

Shared by the owner routes (`api/minecraft.py`, the PWA screen), the debug routes
(`api/debug_minecraft.py`) and the session drain (`minecraft/sessions.py`), so a refusal
reads the same on every surface. The sidecar is host-networked and takes a bearer
(docs/plans/MINECRAFT_BEDROCK_PLAN.md, M0b); lifecycle goes through the supervisor, the
only holder of the docker socket.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from fastapi import HTTPException, Request

SERVICE = "minecraft"
SIDECAR_TIMEOUT_S = 30.0
# A snapshot copies the whole world under `save hold`; a small world takes seconds, but
# the first one after a long session can be slow, and a timeout here would only hide
# whether the hold was released.
SNAPSHOT_TIMEOUT_S = 180.0
# Tests swap in an httpx.MockTransport; None is the real network.
_transport: httpx.AsyncBaseTransport | None = None


def supervisor(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


def base(settings: Any) -> str:
    base = str(settings.minecraft_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="No Minecraft server on this box.")
    return base


async def call(
    settings: Any,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = SIDECAR_TIMEOUT_S,
) -> dict[str, Any]:
    """One call to the wrapper, with its refusals passed through as what they are.

    An unreachable sidecar is a 503 naming the likely cause, because "connection
    refused" on a stopped container reads like a bug when it is a state."""
    token = str(settings.minecraft_token or "")
    if not token:
        # Sending "Bearer " with nothing after it is a malformed header that httpx
        # rejects before any request — the cryptic LocalProtocolError the first deploy
        # hit. Say what is actually missing.
        raise HTTPException(
            status_code=503,
            detail="MINECRAFT_TOKEN is not set for the api — run an update to mint it",
        )
    try:
        async with httpx.AsyncClient(
            base_url=base(settings),
            timeout=timeout_s,
            transport=_transport,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            resp = await client.request(method, path, json=json, params=params)
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Minecraft sidecar unreachable ({type(exc).__name__}) — is it stopped?",
        ) from exc
    if resp.status_code in (401, 503):
        # The sidecar's own auth refusal — a token mismatch is a deploy fault, not a state.
        raise HTTPException(
            status_code=502, detail=f"Minecraft sidecar refused the api: {detail(resp)}"
        )
    if resp.status_code in (400, 404, 409):
        raise HTTPException(status_code=resp.status_code, detail=detail(resp))
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


def detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except ValueError:
        return resp.text


async def _supervisor_call(
    request: Request, settings: Any, method: str, path: str, **kw: Any
) -> httpx.Response:
    """A supervisor call whose failure is a 503 naming the supervisor, not a 500: the
    screen polls this every few seconds and must say what's wrong, not crash."""
    try:
        resp = await supervisor(request).request(
            method,
            path,
            headers={"Authorization": f"Bearer {settings.supervisor_token}"},
            **kw,
        )
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503, detail=f"supervisor unreachable ({type(exc).__name__})"
        ) from exc
    if resp.status_code >= 500:
        raise HTTPException(status_code=503, detail=f"supervisor error {resp.status_code}")
    return resp


async def container(request: Request, settings: Any) -> dict[str, Any] | None:
    resp = await _supervisor_call(request, settings, "GET", "/status")
    resp.raise_for_status()
    for c in resp.json().get("containers", []):
        if c.get("service") == SERVICE:
            return cast(dict[str, Any], c)
    return None


async def lifecycle(request: Request, settings: Any, action: str) -> None:
    resp = await _supervisor_call(
        request, settings, "POST", f"/{action}", json={"service": SERVICE}
    )
    if resp.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail="the minecraft container does not exist yet — run /debug/update",
        )
    resp.raise_for_status()


def _auth(settings: Any) -> dict[str, str]:
    token = str(settings.minecraft_token or "")
    if not token:
        raise HTTPException(
            status_code=503,
            detail="MINECRAFT_TOKEN is not set for the api — run an update to mint it",
        )
    return {"Authorization": f"Bearer {token}"}


async def upload(
    settings: Any, path: str, body: Any, length: int, params: dict[str, str]
) -> dict[str, Any]:
    """Stream an upload (a .mcworld) through to the sidecar without holding it: the api
    stores nothing, so the storage abstraction isn't bypassed — the bytes only pass."""
    headers = {**_auth(settings), "Content-Length": str(length)}
    try:
        async with httpx.AsyncClient(
            base_url=base(settings), timeout=600.0, transport=_transport
        ) as client:
            resp = await client.post(path, content=body, headers=headers, params=params)
    except httpx.TransportError as exc:
        raise HTTPException(
            status_code=503, detail=f"Minecraft sidecar unreachable ({type(exc).__name__})"
        ) from exc
    if resp.status_code in (400, 404, 409):
        raise HTTPException(status_code=resp.status_code, detail=detail(resp))
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


async def download(settings: Any, path: str) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streamed download from the sidecar. The caller streams `resp` and must
    close both (the route hands them to a StreamingResponse background task)."""
    client = httpx.AsyncClient(base_url=base(settings), timeout=600.0, transport=_transport)
    try:
        req = client.build_request("GET", path, headers=_auth(settings))
        resp = await client.send(req, stream=True)
    except httpx.TransportError as exc:
        await client.aclose()
        raise HTTPException(
            status_code=503, detail=f"Minecraft sidecar unreachable ({type(exc).__name__})"
        ) from exc
    if resp.status_code != 200:
        await resp.aread()
        msg = detail(resp)
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=404 if resp.status_code == 404 else 502, detail=msg)
    return client, resp
