"""End-of-turn residency: re-warm the box's hot LLM set after a transient swap.

On a single unified-memory box the recommended set (gpt-oss-120b + qwen3-vl) runs
co-resident as a llama-swap `matrix` set — the box's steady state. A switch to a state
that needs the whole box (an image render frees every LLM; a code session evicts them to
load the coder) is the only thing that displaces it, and the matrix solver then loads
back only the ONE model the next request names — so the OTHER hot member would stay cold
until some later turn happens to need it (the next vision read cold-loading qwen3-vl
mid-turn is exactly the "swapping too often" the owner felt). This coordinator closes
that gap: at the end of a turn it eagerly (re)loads any hot-set member that isn't
resident, so the box returns to 120b + vl rather than waiting to cold-load on demand.

Best-effort throughout: a cloud-only or opted-out box has an empty hot set (every call is
a no-op), and a gateway hiccup is logged, never fatal — residency housekeeping must never
fail or slow a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence

import structlog

from jbrain.config import Settings
from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError

log = structlog.get_logger()


def default_resident_served(settings: Settings) -> list[str]:
    """The served-model names of the box's hot set: the recommended catalog models the
    operator has provisioned, when co-residency is on (the default). Empty when local
    hosting is off, co-residency is opted out, or none of the recommended set is installed
    — in which case the coordinator no-ops. Staged models are intentionally excluded
    (a per-owner runtime pin, not the box's baseline state)."""
    if not (settings.local_llm_enabled and settings.local_llm_resident_group):
        return []
    provisioned = set(settings.local_models)
    return [m.served_model for m in local_catalog.CATALOG if m.recommended and m.id in provisioned]


class ResidencyCoordinator:
    """Re-warms the hot LLM set after a turn that swapped it out (an image render, a code
    session). One instance lives on app.state; the chat endpoint fires `schedule_restore`
    when a turn finishes."""

    def __init__(self, gateway: LocalGateway, default_served: Sequence[str]) -> None:
        self._gateway = gateway
        self._default = list(default_served)
        # Strong refs to the in-flight restore so the loop doesn't GC it mid-load (asyncio
        # holds only weak refs). At most one runs at a time — a fresh schedule while one is
        # in flight is dropped (the running one already re-warms the whole set), which also
        # coalesces a multi-image turn's repeated swaps into a single end-of-turn restore.
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule_restore(self) -> None:
        """Fire-and-forget eager re-warm of the hot set. Non-blocking so it overlaps the
        next turn rather than delaying the reply that just streamed. No-op when the hot set
        is empty (cloud-only / opted-out box) or a restore is already in flight."""
        if not self._default or self._tasks:
            return
        task = asyncio.create_task(self._restore())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _restore(self) -> None:
        """Load each hot-set member that isn't already resident. When the set is already
        hot (the common case — nothing swapped it out) this is one cheap `/running` probe
        and zero loads. A load against a down/cold gateway raises and is suppressed, so the
        worst case is a wasted probe, never a failed turn."""
        running = await self._gateway.running()
        for served in self._default:
            if served not in running:
                with contextlib.suppress(LocalGatewayError):
                    await self._gateway.load(served)
