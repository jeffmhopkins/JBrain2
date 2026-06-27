"""End-of-turn residency: re-warm the box's hot LLM set after a transient swap.

On a single unified-memory box a chosen set of models runs co-resident as a llama-swap
`matrix` set — the box's steady state. Two things put a model in that set: the recommended
pair (gpt-oss-120b + qwen3-vl) when co-residency is on (the default), and any model the
operator has *staged* (an explicit "keep this hot" pin, independent of the flag). A switch
to a state that needs the whole box (an image render frees every LLM; a code session
evicts them to load the coder) is the only thing that displaces the set, and the matrix
solver then loads back only the ONE model the next request names — so the OTHER hot member
would stay cold until some later turn happens to need it (the next vision read cold-loading
qwen3-vl mid-turn is exactly the "swapping too often" the owner felt). This coordinator
closes that gap: at the end of a turn it eagerly (re)loads any hot-set member that isn't
resident, so the box returns to its steady state rather than cold-loading on demand.

Best-effort throughout: a cloud-only or empty-set box no-ops, and a gateway hiccup is
logged, never fatal — residency housekeeping must never fail or slow a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence

import structlog

from jbrain.config import Settings
from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError

log = structlog.get_logger()

# Loads the operator's staged catalog ids (the per-owner runtime pin) at restore time, so a
# freshly staged/unstaged model is reflected without a restart.
StagedLoader = Callable[[], Awaitable[Sequence[str]]]


def default_resident_served(settings: Settings) -> list[str]:
    """The served-model names of the recommended hot set: the recommended catalog models the
    operator has provisioned, when co-residency is on (the default). Empty when local hosting
    is off, co-residency is opted out, or none of the recommended set is installed. The
    STAGED set is layered on separately (the coordinator reads it live) — staging keeps a
    model hot regardless of this flag, so the two sources must not be conflated here."""
    if not (settings.local_llm_enabled and settings.local_llm_resident_group):
        return []
    provisioned = set(settings.local_models)
    return [m.served_model for m in local_catalog.CATALOG if m.recommended and m.id in provisioned]


class ResidencyCoordinator:
    """Re-warms the hot LLM set after a turn that swapped it out (an image render, a code
    session). One instance lives on app.state; the chat endpoint fires `schedule_restore`
    when a turn finishes. The hot set is the recommended pair (when co-residency is on) plus
    the operator's live staged set."""

    def __init__(
        self,
        gateway: LocalGateway,
        recommended_served: Sequence[str],
        *,
        staged_loader: StagedLoader | None = None,
    ) -> None:
        self._gateway = gateway
        self._recommended = list(recommended_served)
        # None on a cloud-only box (no local hosting) — then the recommended set is empty
        # too and the coordinator is inert. Present whenever local hosting is on, so a
        # staged model is re-warmed even with the recommended-set flag off.
        self._staged_loader = staged_loader
        # Strong refs to the in-flight restore so the loop doesn't GC it mid-load (asyncio
        # holds only weak refs). At most one runs at a time — a fresh schedule while one is
        # in flight is dropped (the running one already re-warms the whole set), which also
        # coalesces a multi-image turn's repeated swaps into a single end-of-turn restore.
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule_restore(self) -> None:
        """Fire-and-forget eager re-warm of the hot set. Non-blocking so it overlaps the next
        turn rather than delaying the reply that just streamed. No-op when the box keeps
        nothing resident (cloud-only) or a restore is already in flight."""
        if (not self._recommended and self._staged_loader is None) or self._tasks:
            return
        task = asyncio.create_task(self._restore())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _hot_set(self) -> list[str]:
        """The served names to keep resident: the recommended set plus the live staged set
        (mapped catalog id → served name), de-duplicated, recommended first. A staged-load
        failure degrades to the recommended set rather than breaking housekeeping."""
        served = list(self._recommended)
        if self._staged_loader is not None:
            with contextlib.suppress(Exception):
                for cid in await self._staged_loader():
                    model = local_catalog.get(cid)
                    if model is not None and model.served_model not in served:
                        served.append(model.served_model)
        return served

    async def _restore(self) -> None:
        """Load each hot-set member that isn't already resident. When the set is already hot
        (the common case — nothing swapped it out) this is one cheap `/running` probe and
        zero loads. A load against a down/cold gateway raises and is suppressed, so the worst
        case is a wasted probe, never a failed turn."""
        hot = await self._hot_set()
        if not hot:
            return
        running = await self._gateway.running()
        for served in hot:
            if served not in running:
                with contextlib.suppress(LocalGatewayError):
                    await self._gateway.load(served)
