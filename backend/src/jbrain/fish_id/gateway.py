"""Management client for the fishial service — runtime memory state.

The sibling of jbrain.image_gen.gateway, for the fish side. This is NOT an
identification call (that's jbrain.fish_id.client), so it lives apart: it speaks the
service's admin HTTP API to report reachability/load state and to free the model:
  - GET  /health  → reachability (+ whether weights are currently loaded)
  - POST /free    → unload the model and free unified memory

The model is load → use → unload per call (the owner's decision): the service loads
weights lazily on /identify and the tool POSTs /free right after, so it is never
resident between identifications — the `_free_comfyui_model` pattern. `status()` is
best-effort and swallows every error (the settings screen must render with the
service down or absent); `free()` surfaces failures because a caller asked for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger()


class FishIdGatewayError(Exception):
    """A free/management call the fishial service rejected or couldn't be reached for."""


class FishIdMemory(Protocol):
    """The free-memory capability the identify tool depends on, so it takes the
    action rather than the concrete HTTP client (the in-memory test fake satisfies
    it — the same seam as `ComfyUiMemory`)."""

    async def free(self) -> None: ...


@dataclass(frozen=True)
class FishIdStatus:
    """A point-in-time read of the fishial service. `loaded` is None when the service
    is unreachable or didn't report it (an older build)."""

    reachable: bool
    loaded: bool | None = None


class FishIdGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
    ):
        self._root = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout

    async def status(self) -> FishIdStatus:
        """Service reachability (+ loaded state), or an unreachable status on ANY
        failure (down, non-2xx, or malformed) — runtime state never blocks the
        settings screen."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/health")
                resp.raise_for_status()
                return FishIdStatus(reachable=True, loaded=_parse_loaded(resp.json()))
        except (httpx.HTTPError, ValueError) as exc:
            log.info("fish_id_gateway.status_unavailable", error=str(exc))
            return FishIdStatus(reachable=False)

    async def free(self) -> None:
        """Unload the model and free memory. Raises FishIdGatewayError on any
        failure (a caller asked for it, so a failure is surfaced — but the identify
        path treats it as best-effort, since the image is already in hand)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._root}/free")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise FishIdGatewayError(str(exc)) from exc


def _parse_loaded(payload: object) -> bool | None:
    """Read a boolean `loaded` flag from /health, or None when absent/odd."""
    if isinstance(payload, dict):
        value = payload.get("loaded")
        if isinstance(value, bool):
            return value
    return None
