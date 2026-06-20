"""Management client for the ComfyUI image service — runtime memory state.

The sibling of jbrain.llm.local_gateway, for the image side. This is NOT a
generation call (that's jbrain.image_gen.comfyui), so it lives apart: it speaks
ComfyUI's admin HTTP API to report and free GPU/unified memory:
  - GET  /system_stats   → reachability + real VRAM total/free (Strix Halo's iGPU
                           shares system RAM, so this is the true headroom)
  - POST /free           → unload cached models and free memory

Unlike llama-swap, ComfyUI has no per-model "loaded" list and no explicit load
endpoint: a model loads on the first generation that needs it and stays cached
until evicted, so "warm/load" is just running a workflow (the generate path) and
the only management action here is freeing. `status()` is best-effort and
swallows every error (the settings screen must render with the service down or
absent); `free()` surfaces failures because the operator explicitly asked for it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

_BYTES_PER_GB = 1024**3


class ComfyUiGatewayError(Exception):
    """A free/management call ComfyUI rejected or couldn't be reached for."""


@dataclass(frozen=True)
class GatewayStatus:
    """A point-in-time read of the ComfyUI service. `vram_*_gb` are None when the
    service is unreachable or didn't report device stats (an older build)."""

    reachable: bool
    vram_total_gb: float | None = None
    vram_free_gb: float | None = None


class ComfyUiGatewayClient:
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

    async def status(self) -> GatewayStatus:
        """Service reachability + real VRAM headroom, or an unreachable status on
        ANY failure (down, non-2xx, or malformed) — runtime state never blocks the
        settings screen."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/system_stats")
                resp.raise_for_status()
                total, free = _parse_vram(resp.json())
                return GatewayStatus(reachable=True, vram_total_gb=total, vram_free_gb=free)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("comfyui_gateway.status_unavailable", error=str(exc))
            return GatewayStatus(reachable=False)

    async def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None:
        """Unload cached models and/or free memory. Raises ComfyUiGatewayError on
        any failure (the operator asked for it, so a failure is surfaced)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._root}/free",
                    json={"unload_models": unload_models, "free_memory": free_memory},
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ComfyUiGatewayError(str(exc)) from exc

    async def interrupt(self) -> None:
        """Stop the currently-running generation (ComfyUI POST /interrupt). The box
        runs one job at a time, so this unambiguously cancels the in-flight render;
        the blocked generate/edit await then returns. Raises ComfyUiGatewayError on
        any failure (an explicit operator stop, so a failure is surfaced)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._root}/interrupt")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ComfyUiGatewayError(str(exc)) from exc


def _parse_vram(payload: object) -> tuple[float | None, float | None]:
    """Sum vram_total / vram_free (bytes) across the devices /system_stats reports,
    in GB. Tolerant of an older/odd shape: returns (None, None) when no device
    carries numeric VRAM fields."""
    if not isinstance(payload, dict):
        return None, None
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return None, None
    total = 0.0
    free = 0.0
    saw = False
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        vt, vf = dev.get("vram_total"), dev.get("vram_free")
        if isinstance(vt, int | float) and isinstance(vf, int | float):
            total += float(vt)
            free += float(vf)
            saw = True
    if not saw:
        return None, None
    return round(total / _BYTES_PER_GB, 2), round(free / _BYTES_PER_GB, 2)
