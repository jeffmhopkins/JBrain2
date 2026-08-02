"""ComfyUI liveness → which image tools an agent turn may see.

The image tools are wired whenever a ComfyUI URL is configured, but a configured
server can still be *down*. This caches a reachability probe so the agent loop can
HIDE the image-**generation** tools (`generate_image`, `edit_image`) from a turn
when ComfyUI is unreachable — the model never sees a tool it can't run, so it
reaches for a chart tool instead of a dead generator. `analyze_image` is kept even
when the server is down: it's a read that degrades to the on-box OCR pass, not a
generator, so removing it would lose a working capability.

One cached bool with a short TTL keeps the per-turn cost at zero on the hot path
(a probe fires at most once per `ttl_s`); the loop awaits `hidden_tools()` each
turn and folds the result into the registry's per-turn visibility gate.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol

import structlog

log = structlog.get_logger()

# The GENERATION tools hidden when ComfyUI is down. analyze_image is deliberately
# absent — it degrades to OCR and stays available (owner's call).
_HIDDEN_WHEN_DOWN = frozenset({"generate_image", "edit_image"})


class _Probe(Protocol):
    """The one thing the liveness cache needs — a reachability read (the
    `ComfyUiGatewayClient.status()` seam; the test fake satisfies it too)."""

    async def status(self) -> object: ...  # returns an object with a `.reachable` bool


class ImageGenLiveness:
    """Caches ComfyUI reachability; answers which image tools to hide this turn.

    Optimistic on cold start (reachable until the first probe resolves) so a slow
    first probe never hides a healthy server. Probes are throttled to one per
    `ttl_s` and serialized by a lock, so concurrent turns share a single in-flight
    check rather than stampeding the gateway.
    """

    def __init__(
        self,
        gateway: _Probe,
        *,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._gateway = gateway
        self._ttl = ttl_s
        self._clock = clock
        self._reachable = True
        self._checked_at: float | None = None
        self._lock = asyncio.Lock()

    async def hidden_tools(self) -> frozenset[str]:
        """The image-gen tool names to hide this turn — empty when ComfyUI is
        reachable (or within the freshness window of a reachable probe)."""
        await self._refresh()
        return frozenset() if self._reachable else _HIDDEN_WHEN_DOWN

    async def _refresh(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():  # a concurrent turn may have refreshed while we waited
                return
            status = await self._gateway.status()
            self._reachable = bool(getattr(status, "reachable", False))
            self._checked_at = self._clock()
            if not self._reachable:
                log.info("image_liveness.comfyui_down", hidden=sorted(_HIDDEN_WHEN_DOWN))

    def _fresh(self) -> bool:
        return self._checked_at is not None and self._clock() - self._checked_at < self._ttl
