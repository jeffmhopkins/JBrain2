"""End-of-turn residency: re-warm the box's hot LLM set after a transient swap.

On a single unified-memory box a chosen set of models runs co-resident as a llama-swap
non-swapping group — the box's steady state. Two things put a model in that group: the
recommended pair (gpt-oss-120b + qwen3-vl) when co-residency is on (the default), and any
model the operator has *staged* (an explicit "keep this hot" pin, independent of the flag).
A switch to a state that needs the whole box (an image render frees every LLM; a code
session evicts them to load the coder) is the only thing that displaces the group, and the
gateway then loads back only the ONE model the next request names — so the OTHER hot member
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
from collections.abc import Awaitable, Callable, Mapping, Sequence

import structlog

from jbrain.config import Settings
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.llm.local_weights import weights_size_gb

log = structlog.get_logger()

# Loads the operator's staged catalog ids (the per-owner runtime pin) at restore time, so a
# freshly staged/unstaged model is reflected without a restart.
StagedLoader = Callable[[], Awaitable[Sequence[str]]]
# Loads the live per-model context-window overrides (catalog id → tokens), so the memory
# budget sizes each model's KV against the window it actually serves.
WindowsLoader = Callable[[], Awaitable[Mapping[str, int]]]


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
        windows_loader: WindowsLoader | None = None,
        models_dir: str = "",
        budget_enabled: bool = False,
        free_ram_fraction: float = 0.25,
    ) -> None:
        self._gateway = gateway
        self._recommended = list(recommended_served)
        # None on a cloud-only box (no local hosting) — then the recommended set is empty
        # too and the coordinator is inert. Present whenever local hosting is on, so a
        # staged model is re-warmed even with the recommended-set flag off.
        self._staged_loader = staged_loader
        # Memory-budget inputs (co-residency mode). `budget_enabled` mirrors
        # local_llm_resident_group: OFF → ensure_room is a no-op and the gateway swaps one
        # at a time (old behavior); ON → the app evicts to hold the free-RAM floor. The
        # windows loader + models dir let the budget size each model's real footprint
        # (measured weights + KV at the served window).
        self._windows_loader = windows_loader
        self._models_dir = models_dir
        self._budget_enabled = budget_enabled
        self._free_ram_fraction = free_ram_fraction
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

    async def _windows(self) -> Mapping[str, int]:
        """Live per-model context-window overrides (catalog id → tokens); empty when no
        loader is wired or the read fails, so the budget falls back to catalog defaults."""
        if self._windows_loader is None:
            return {}
        with contextlib.suppress(Exception):
            return await self._windows_loader()
        return {}

    async def _staged_ids(self) -> set[str]:
        """The operator's staged catalog ids (protected from eviction until last)."""
        if self._staged_loader is None:
            return set()
        with contextlib.suppress(Exception):
            return set(await self._staged_loader())
        return set()

    async def _footprint(self, served_model: str, windows: Mapping[str, int]) -> float:
        """A resident model's unified-memory footprint (GiB) at its served window —
        measured weights + KV. 0.0 for a served name outside the catalog: we can't size
        it, so it never drives (or blocks) an eviction."""
        model = local_catalog.get_by_served(served_model)
        if model is None:
            return 0.0
        window = windows.get(model.id, model.context_window)
        disk = weights_size_gb(self._models_dir, model.id) if self._models_dir else None
        return local_catalog.footprint_gb(model, window, disk_gb=disk)

    async def ensure_room(self, served_model: str) -> None:
        """Before `served_model` loads, evict the FEWEST resident models needed to keep the
        free-RAM floor after it's resident — biggest-footprint first, staged models last.
        A no-op when co-residency is off, the model is already resident, or it fits without
        eviction. Best-effort throughout: any probe/evict/meminfo failure is swallowed, so
        residency housekeeping never fails or slows the turn it precedes (the router awaits
        this on the local completion path)."""
        if not self._budget_enabled:
            return  # one-at-a-time swap mode — the gateway manages residency itself
        try:
            running = await self._gateway.running()
            if served_model in running:
                return
            mem = read_memory_gb()
            if mem is None:
                return  # can't measure RAM — don't evict blindly
            total, used = mem
            ceiling = total * (1.0 - self._free_ram_fraction)  # keep used at/under this
            windows = await self._windows()
            predicted = used + await self._footprint(served_model, windows)
            if predicted <= ceiling:
                return  # fits alongside what's resident — evict nothing
            staged = await self._staged_ids()
            # Rank eviction candidates: non-staged before staged, then biggest footprint
            # first, so freeing the room costs the fewest evictions and spares the tiny /
            # explicitly-pinned models (evict one big model, not several small ones).
            victims = []
            for served in running:
                if served == served_model:
                    continue
                cid = local_catalog.id_for_served(served)
                fp = await self._footprint(served, windows)
                victims.append(((cid in staged), -fp, served, fp))
            victims.sort()
            freed = 0.0
            for _staged, _neg_fp, served, fp in victims:
                if predicted - freed <= ceiling:
                    break
                with contextlib.suppress(LocalGatewayError):
                    await self._gateway.unload(served)
                    freed += fp
        except Exception as exc:  # noqa: BLE001 — residency housekeeping never fails a turn
            log.warning("residency.ensure_room_failed", model=served_model, error=repr(exc))

    async def _restore(self) -> None:
        """Re-warm hot-set members that aren't resident. When the set is already hot (the
        common case) this is one cheap `/running` probe and zero loads. In co-residency mode
        a member is loaded only while it FITS alongside what's resident (opportunistic — it
        never evicts another hot member to squeeze one in; on-demand loads go through
        ensure_room instead). A load against a down/cold gateway is suppressed, so the worst
        case is a wasted probe, never a failed turn."""
        hot = await self._hot_set()
        if not hot:
            return
        running = await self._gateway.running()
        if not self._budget_enabled:
            for served in hot:
                if served not in running:
                    with contextlib.suppress(LocalGatewayError):
                        await self._gateway.load(served)
            return
        mem = read_memory_gb()
        if mem is None:
            return  # can't budget the re-warm — leave cold members to load on demand
        total, used = mem
        ceiling = total * (1.0 - self._free_ram_fraction)
        windows = await self._windows()
        for served in hot:
            if served in running:
                continue
            fp = await self._footprint(served, windows)
            if used + fp > ceiling:
                continue  # no room without evicting a hot member — load it on demand
            with contextlib.suppress(LocalGatewayError):
                await self._gateway.load(served)
                used += fp  # bound the pass so several missing members don't over-commit
