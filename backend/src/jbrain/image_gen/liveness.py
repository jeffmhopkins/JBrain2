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

Hiding is HYSTERETIC, not instantaneous: the tool array is rendered into the
prompt's leading tokens (--jinja), so flipping it invalidates the whole KV prefix —
a single flapped probe used to cost a full ~37k-token re-prefill (~170 s on qwen,
observed 2026-08-24) to save one failed tool call. The tools now hide only after
ComfyUI has been down CONTINUOUSLY for `hide_after_s`; inside that window the model
keeps seeing them and a call simply fails with the tool's own clean error. A real
outage still hides them within minutes.
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
        hide_after_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._gateway = gateway
        self._ttl = ttl_s
        self._hide_after = hide_after_s
        self._clock = clock
        self._reachable = True
        self._checked_at: float | None = None
        # When the CURRENT unbroken run of failed probes began; None while reachable.
        # Hiding keys off this, not off the latest probe — see the module docstring.
        self._down_since: float | None = None
        self._lock = asyncio.Lock()

    async def hidden_tools(self) -> frozenset[str]:
        """The image-gen tool names to hide this turn — empty when ComfyUI is
        reachable, and STILL empty through a short outage (the hysteresis window):
        only a sustained outage reshapes the prompt's tool array."""
        await self._refresh()
        if self._down_since is None:
            return frozenset()
        if self._clock() - self._down_since < self._hide_after:
            return frozenset()  # flap tolerance: keep the prompt (and its KV) stable
        return _HIDDEN_WHEN_DOWN

    async def _refresh(self) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():  # a concurrent turn may have refreshed while we waited
                return
            status = await self._gateway.status()
            self._reachable = bool(getattr(status, "reachable", False))
            self._checked_at = self._clock()
            if self._reachable:
                if self._down_since is not None:
                    log.info("image_liveness.comfyui_recovered")
                self._down_since = None
            elif self._down_since is None:
                self._down_since = self._clock()
                log.info(
                    "image_liveness.comfyui_down",
                    hidden_after_s=self._hide_after,
                    hidden=sorted(_HIDDEN_WHEN_DOWN),
                )

    def _fresh(self) -> bool:
        return self._checked_at is not None and self._clock() - self._checked_at < self._ttl
