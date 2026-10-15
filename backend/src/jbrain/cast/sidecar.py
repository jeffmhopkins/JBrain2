"""The api's one way to the host-networked `cast` sidecar (deploy/cast/server.py).

The sidecar takes `cast.token` from the internal secrets; this reads it per call so a
re-mint needs no restart. Failures come back as `HTTPException`s the routes pass on: 503
when the sidecar is not there, 409 with the gate's verdict when a TV is in use, and 400/404
as the sidecar said them.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from fastapi import HTTPException, Request

from jbrain.api import internal_auth
from jbrain.storage import InternalSecrets

SIDECAR_TIMEOUT_S = 15.0
# Tests swap in an httpx.MockTransport; None is the real network.
_transport: httpx.AsyncBaseTransport | None = None


class Sidecar:
    """The sidecar at `base_url`, authenticated with the token `secrets` holds."""

    def __init__(self, secrets: InternalSecrets, base_url: str) -> None:
        self._secrets = secrets
        self._base = base_url.strip().rstrip("/")

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._base:
            raise HTTPException(status_code=503, detail="No cast sidecar on this box.")
        token = self._secrets.read(internal_auth.CAST)
        if not token:
            raise HTTPException(status_code=503, detail="cast.token is not minted yet")
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                timeout=SIDECAR_TIMEOUT_S,
                transport=_transport,
                headers={"Authorization": f"Bearer {token.decode()}"},
            ) as client:
                resp = await client.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"cast sidecar unreachable ({type(exc).__name__}) — is it running?",
            ) from exc
        try:
            body = resp.json()
        except ValueError:
            body = {"error": resp.text[:300]}
        if resp.status_code == 401:
            raise HTTPException(status_code=502, detail="cast sidecar refused the api's token")
        if resp.status_code in (400, 404, 409):
            # A gate refusal carries the verdict that refused it; pass it through whole.
            raise HTTPException(status_code=resp.status_code, detail=body)
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=body)
        return cast(dict[str, Any], body)


def for_request(request: Request, settings: Any) -> Sidecar:
    secrets = cast(InternalSecrets, request.app.state.internal_secrets)
    return Sidecar(secrets, str(settings.cast_url or ""))


async def call(
    request: Request,
    settings: Any,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await for_request(request, settings).call(method, path, json=json, params=params)
