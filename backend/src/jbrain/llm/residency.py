"""Residency: the app is the single evictor of the unified-memory box, and it restores.

On a single unified-memory box (Strix Halo) every local model shares one RAM pool, so
loading one can require unloading others. The gateway (llama-swap) is configured to never
evict on its own (every model is a `swap: false` member) — this coordinator is the sole
evictor. Two duties:

  - `ensure_room` (on the local completion path, awaited by the router before each load):
    make room for the model a request needs. If it wouldn't fit under the free-RAM floor,
    evict the fewest resident models — biggest-footprint first, operator-STAGED models last
    — until it does. A model bigger than the whole floor still loads: it evicts everything
    and gets the box to itself. That is the whole paradigm: load any model, unload until it
    fits.
  - `schedule_restore` (fired at end of turn): put back what a transient displacement took.
    Every eviction — here, a code session giving the coder the box, an image render freeing
    the LLMs — records the served names it removed (`note_evicted`), and restore reloads
    those (plus the staged set) as far as the budget allows, so the box drifts back to the
    steady state it had before the displacement rather than cold-loading on demand.

The keep-hot set is therefore not a fixed pair — it's whatever was resident before the last
displacement, remembered and restored. Staging layers on top as an explicit "keep this hot
and evict it last".

Best-effort throughout: a cloud-only or disabled box no-ops, and any gateway/meminfo hiccup
is swallowed and logged — residency housekeeping must never fail or slow a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

import structlog

from jbrain.host_metrics import read_memory_gb
from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.llm.local_weights import weights_size_gb

log = structlog.get_logger()

# Loads the operator's staged catalog ids (the per-owner runtime pin) live, so a freshly
# staged/unstaged model is reflected without a restart.
StagedLoader = Callable[[], Awaitable[Sequence[str]]]
# Loads the live per-model context-window overrides (catalog id → tokens), so the memory
# budget sizes each model's KV against the window it actually serves.
WindowsLoader = Callable[[], Awaitable[Mapping[str, int]]]


class ResidencyCoordinator:
    """The box's sole model evictor and restorer. One instance lives on app.state: the
    router awaits `ensure_room` before every local completion, and the chat endpoint (plus
    the code power-off and image render paths) fires `schedule_restore` when a turn finishes.
    The set to restore is remembered dynamically — the models evicted since the last restore
    (`note_evicted`) plus the operator's live staged set."""

    def __init__(
        self,
        gateway: LocalGateway,
        *,
        staged_loader: StagedLoader | None = None,
        windows_loader: WindowsLoader | None = None,
        models_dir: str = "",
        enabled: bool = False,
        free_ram_fraction: float = 0.25,
    ) -> None:
        self._gateway = gateway
        # Inert on a cloud-only box (no local hosting): ensure_room/restore no-op and nothing
        # is ever recorded. Mirrors settings.local_llm_enabled.
        self._enabled = enabled
        # None on a cloud-only box; present whenever local hosting is on so a staged model is
        # kept hot (and evicted last) regardless of what else has been displaced.
        self._staged_loader = staged_loader
        self._windows_loader = windows_loader
        self._models_dir = models_dir
        self._free_ram_fraction = free_ram_fraction
        # Served names evicted (by us or by another displacement) and awaiting restore. The
        # box's remembered steady state minus whatever currently holds the RAM. Bounded by the
        # provisioned model count; entries clear as they reload or are attempted.
        self._displaced: set[str] = set()
        # Strong refs to the in-flight restore so the loop doesn't GC it mid-load (asyncio
        # holds only weak refs). At most one runs at a time — a fresh schedule while one is in
        # flight is dropped (the running one already restores the whole set), which coalesces a
        # multi-image turn's repeated displacements into a single end-of-turn restore.
        self._tasks: set[asyncio.Task[None]] = set()

    def note_evicted(self, served_names: Iterable[str]) -> None:
        """Record models an external displacement (a code session, an image render) unloaded,
        so the next restore puts them back. Only known catalog models are tracked — an
        unrecognised served name can't be sized or reloaded, so it's ignored. Cheap and
        synchronous; a no-op on a disabled box."""
        if not self._enabled:
            return
        for name in served_names:
            if local_catalog.get_by_served(name) is not None:
                self._displaced.add(name)

    def schedule_restore(self) -> None:
        """Fire-and-forget restore of the displaced + staged set. Non-blocking so it overlaps
        the next turn rather than delaying the reply that just streamed. No-op when disabled,
        when nothing is displaced and no staged set is wired, or when a restore is already in
        flight."""
        if not self._enabled or self._tasks:
            return
        if not self._displaced and self._staged_loader is None:
            return
        task = asyncio.create_task(self._restore())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _windows(self) -> Mapping[str, int]:
        """Live per-model context-window overrides (catalog id → tokens); empty when no loader
        is wired or the read fails, so the budget falls back to catalog defaults."""
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

    async def _staged_served(self) -> list[str]:
        """The staged set as served names (catalog id → served), so restore keeps it hot."""
        served: list[str] = []
        for cid in await self._staged_ids():
            model = local_catalog.get(cid)
            if model is not None:
                served.append(model.served_model)
        return served

    async def _footprint(self, served_model: str, windows: Mapping[str, int]) -> float:
        """A resident model's unified-memory footprint (GiB) at its served window — measured
        weights + KV. 0.0 for a served name outside the catalog: we can't size it, so it never
        drives (or blocks) an eviction."""
        model = local_catalog.get_by_served(served_model)
        if model is None:
            return 0.0
        window = windows.get(model.id, model.context_window)
        disk = weights_size_gb(self._models_dir, model.id) if self._models_dir else None
        return local_catalog.footprint_gb(model, window, disk_gb=disk)

    async def ensure_room(self, served_model: str) -> None:
        """Before `served_model` loads, evict the FEWEST resident models needed to keep the
        free-RAM floor after it's resident — biggest-footprint first, staged models last — and
        record each eviction so the next restore can put it back. A no-op when the model is
        already resident or it fits without eviction; a model larger than the whole floor
        evicts everything and takes the box. Best-effort: any probe/evict/meminfo failure is
        swallowed, so residency housekeeping never fails or slows the turn it precedes (the
        router awaits this on the local completion path)."""
        if not self._enabled:
            return
        try:
            # It's being loaded for active use now, so it's no longer awaiting restore.
            self._displaced.discard(served_model)
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
            # Rank eviction candidates: non-staged before staged, then biggest footprint first,
            # so freeing the room costs the fewest evictions and spares the tiny / explicitly
            # pinned models (evict one big model, not several small ones).
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
                    self._displaced.add(served)  # remember it for the end-of-turn restore
        except Exception as exc:  # noqa: BLE001 — residency housekeeping never fails a turn
            log.warning("residency.ensure_room_failed", model=served_model, error=repr(exc))

    async def _restore(self) -> None:
        """Reload the displaced + staged set that isn't already resident, as far as the budget
        allows (opportunistic — it never evicts to squeeze a member in; on-demand loads go
        through ensure_room). A member that fits is loaded and cleared from the displaced set;
        one that doesn't fit is left for a later restore, once whatever holds the RAM is gone.
        A load against a down/cold gateway is suppressed, so the worst case is a wasted probe,
        never a failed turn."""
        if not self._enabled:
            return
        staged = set(await self._staged_served())
        targets = self._displaced | staged
        if not targets:
            return
        running = await self._gateway.running()
        mem = read_memory_gb()
        if mem is None:
            return  # can't budget the restore — leave cold members to load on demand
        total, used = mem
        ceiling = total * (1.0 - self._free_ram_fraction)
        windows = await self._windows()
        # Deterministic, keep-hot-favouring order when not everything fits: staged members
        # first, then biggest footprint first (bring back the model the turn was actually using
        # before a smaller one). A bare set would restore an arbitrary subset.
        scored = []
        for served in targets:
            fp = await self._footprint(served, windows)
            scored.append((served not in staged, -fp, served, fp))
        scored.sort()
        for _not_staged, _neg_fp, served, fp in scored:
            if served in running:
                self._displaced.discard(served)  # already back
                continue
            if used + fp > ceiling:
                continue  # no room without evicting a resident model — leave it for later
            with contextlib.suppress(LocalGatewayError):
                await self._gateway.load(served)
                used += fp  # bound the pass so several missing members don't over-commit
            # Attempted (loaded, or the gateway refused) → no longer pending. A staged member
            # re-enters via _staged_served next restore; a transient miss is re-displaced when
            # it's next evicted, so we never spin retrying a since-removed model.
            self._displaced.discard(served)
