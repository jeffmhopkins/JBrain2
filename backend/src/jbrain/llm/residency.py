"""Residency: the app is the single evictor of the unified-memory box, and it restores.

On a single unified-memory box (Strix Halo) every local model shares one RAM pool, so
loading one can require unloading others. The gateway (llama-swap) is configured to never
evict on its own (every model is a `swap: false` member) — this coordinator is the sole
evictor. Three duties:

  - `plan_load` (dry-run): compute what loading a model would cost RIGHT NOW — which
    resident models would be evicted to hold the free-RAM floor, and the projected
    footprint — with no side effects. The settings screen's "stage" preview calls this so
    the operator sees the eviction before committing the load.
  - `ensure_room` (on the local completion path, awaited by the router before each load)
    and `free_room` (the operator's deliberate load from the settings screen): make room
    for a model. If it wouldn't fit under the free-RAM floor, evict the fewest resident
    models — biggest-footprint first — until it does. A model bigger than the whole floor
    still loads: it evicts everything and gets the box to itself. That is the whole
    paradigm: load any model, unload until it fits. The two differ only in bookkeeping —
    ensure_room records each eviction as a TRANSIENT displacement to restore at end of
    turn; free_room does NOT, because the operator's manual load is a deliberate change to
    the steady state, not a displacement to undo.
  - `schedule_restore` (fired at end of turn): put back what a transient displacement took.
    Every ensure_room eviction — plus a code session giving the coder the box, an image
    render freeing the LLMs — records the served names it removed (`note_evicted`), and
    restore reloads those as far as the budget allows, so the box drifts back to the steady
    state it had before the displacement rather than cold-loading on demand.

The keep-hot set is therefore not a fixed pin — it's whatever was resident before the last
displacement, remembered and restored. (There is no explicit operator pin: a model the
operator uses stays warm on its own via this restore, and a deliberate manual load via
free_room is left in place rather than proactively displaced.)

Best-effort throughout: a cloud-only or disabled box no-ops, and any gateway/meminfo hiccup
is swallowed and logged — residency housekeeping must never fail or slow a turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

import structlog

from jbrain.host_metrics import read_memory_gb
from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.llm.local_weights import weights_size_gb

log = structlog.get_logger()

# Loads the live per-model context-window overrides (catalog id → tokens), so the memory
# budget sizes each model's KV against the window it actually serves.
WindowsLoader = Callable[[], Awaitable[Mapping[str, int]]]


class ResidencyError(Exception):
    """A deliberate refusal to load a model — distinct from the best-effort housekeeping
    errors that are swallowed. Raised when a model can't physically fit the box even after
    evicting everything (its footprint alone exceeds total RAM): loading it would drive the
    box into an out-of-memory hard-freeze, so the load is refused rather than attempted. The
    caller surfaces it (a 409 on the manual load, a failed completion on the router path)."""


@dataclass(frozen=True)
class EvictionPlan:
    """What loading `target` (a served name) would cost right now — computed from the live
    gateway + memory reading, with NO side effects. Shared by the dry-run preview and the
    two eviction paths, so the preview is exactly what the load will do."""

    target: str
    # Served names that would be evicted, biggest-footprint first, to hold the free-RAM
    # floor after `target` is resident. Empty when it fits (or is already resident).
    victims: tuple[str, ...]
    # Measured used memory now (GiB), and the projected used after the load + evictions.
    resident_gb: float
    projected_gb: float
    # The free-RAM floor: used memory must stay at/under this (total * (1 - free_fraction)).
    ceiling_gb: float
    total_gb: float
    # Loads with no eviction (fits under the floor, or already resident).
    fits: bool
    # Even evicting every candidate leaves it over the floor — it takes the box alone.
    over: bool
    # Even evicting everything, the model's footprint exceeds TOTAL RAM: it physically can't
    # fit and loading it would OOM-crash the box. The load must be refused, not attempted.
    over_box: bool
    already_resident: bool


class ResidencyCoordinator:
    """The box's sole model evictor and restorer. One instance lives on app.state: the
    router awaits `ensure_room` before every local completion, the settings screen calls
    `plan_load`/`free_room` for its stage-preview and manual load, and the chat endpoint
    (plus the code power-off and image render paths) fires `schedule_restore` when a turn
    finishes. The set to restore is remembered dynamically — the models evicted since the
    last restore (`note_evicted`)."""

    def __init__(
        self,
        gateway: LocalGateway,
        *,
        windows_loader: WindowsLoader | None = None,
        models_dir: str = "",
        enabled: bool = False,
        free_ram_fraction: float = 0.25,
    ) -> None:
        self._gateway = gateway
        # Inert on a cloud-only box (no local hosting): ensure_room/restore no-op and nothing
        # is ever recorded. Mirrors settings.local_llm_enabled.
        self._enabled = enabled
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
        """Fire-and-forget restore of the displaced set. Non-blocking so it overlaps the next
        turn rather than delaying the reply that just streamed. No-op when disabled, when
        nothing is displaced, or when a restore is already in flight."""
        if not self._enabled or self._tasks:
            return
        if not self._displaced:
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

    async def _plan(self, served_model: str) -> EvictionPlan | None:
        """Compute what loading `served_model` would cost right now — the eviction plan —
        with no side effects. None when disabled or the RAM reading is unavailable (can't
        project blindly). Shared by plan_load (dry-run) and the two eviction paths, so the
        preview matches what the load does. Ranks victims biggest-footprint first: freeing
        the room costs the fewest evictions and spares the tiny models (evict one big model,
        not several small ones)."""
        if not self._enabled:
            return None
        running = await self._gateway.running()
        mem = read_memory_gb()
        if mem is None:
            return None
        total, used = mem
        ceiling = total * (1.0 - self._free_ram_fraction)  # keep used at/under this
        windows = await self._windows()
        if served_model in running:
            return EvictionPlan(
                target=served_model,
                victims=(),
                resident_gb=used,
                projected_gb=used,
                ceiling_gb=ceiling,
                total_gb=total,
                fits=True,
                over=False,
                over_box=False,
                already_resident=True,
            )
        predicted = used + await self._footprint(served_model, windows)
        if predicted <= ceiling:  # fits alongside what's resident — evict nothing
            return EvictionPlan(
                target=served_model,
                victims=(),
                resident_gb=used,
                projected_gb=predicted,
                ceiling_gb=ceiling,
                total_gb=total,
                fits=True,
                over=False,
                over_box=False,
                already_resident=False,
            )
        # Rank eviction candidates biggest-footprint first (a generator with `await` can't be
        # sorted directly — build the list, then sort).
        ranked: list[tuple[float, str]] = []
        for served in running:
            if served == served_model:
                continue
            ranked.append((-await self._footprint(served, windows), served))
        ranked.sort()
        victims: list[str] = []
        freed = 0.0
        for neg_fp, served in ranked:
            if predicted - freed <= ceiling:
                break
            victims.append(served)
            freed += -neg_fp
        projected = predicted - freed
        return EvictionPlan(
            target=served_model,
            victims=tuple(victims),
            resident_gb=used,
            projected_gb=projected,
            ceiling_gb=ceiling,
            total_gb=total,
            fits=False,
            over=projected > ceiling,
            # Even after evicting everything, the model won't fit in physical RAM.
            over_box=projected > total,
            already_resident=False,
        )

    async def plan_load(self, served_model: str) -> EvictionPlan | None:
        """The dry-run: what would loading `served_model` evict, and where would the box
        land? No side effects. None on a disabled box or when RAM can't be measured. The
        settings screen's stage-preview surfaces this before the operator commits the load.
        Best-effort — a gateway/meminfo hiccup surfaces as None, never a raise."""
        if not self._enabled:
            return None
        try:
            return await self._plan(served_model)
        except Exception as exc:  # noqa: BLE001 — a preview must never raise
            log.warning("residency.plan_load_failed", model=served_model, error=repr(exc))
            return None

    def _refuse_if_over_box(self, plan: EvictionPlan) -> None:
        """Raise ResidencyError when the plan can't fit the box (footprint > total RAM even
        after evicting everything). Called before any eviction, so we never destroy resident
        models to make room for a load that would only OOM-crash the box. The distinct
        exception (not swallowed like a housekeeping hiccup) is surfaced to the caller."""
        if plan.over_box:
            raise ResidencyError(
                f"{plan.target} needs ~{plan.projected_gb:.0f} GB but the box has only "
                f"{plan.total_gb:.0f} GB — refusing to load (it would run out of memory)."
            )

    async def ensure_room(self, served_model: str) -> None:
        """Before `served_model` loads on the completion path, evict the fewest resident
        models needed to hold the free-RAM floor after it's resident, and record each
        eviction as a TRANSIENT displacement so the end-of-turn restore can put it back. A
        no-op when already resident or it fits; a model larger than the whole floor evicts
        everything and takes the box — UNLESS it can't fit the box at all, in which case it
        raises ResidencyError instead of loading into an OOM. Probe/evict/meminfo hiccups are
        swallowed (housekeeping never fails a turn); the deliberate over-box refusal is not."""
        if not self._enabled:
            return
        try:
            plan = await self._plan(served_model)
        except Exception as exc:  # noqa: BLE001 — housekeeping hiccup: best-effort, no-op
            log.warning("residency.ensure_room_failed", model=served_model, error=repr(exc))
            return
        if plan is None:
            return
        self._refuse_if_over_box(plan)  # raises before we evict anything
        # It's being loaded for active use now, so it's no longer awaiting restore.
        self._displaced.discard(served_model)
        for served in plan.victims:
            with contextlib.suppress(LocalGatewayError):
                await self._gateway.unload(served)
                self._displaced.add(served)  # remember it for the end-of-turn restore

    async def free_room(self, served_model: str) -> None:
        """Make room for a DELIBERATE operator load (the settings screen's stage → Load):
        evict the same fewest-biggest set ensure_room would, but do NOT record the evictions
        for restore — a manual load is a change to the steady state, not a transient
        displacement to undo (else the next turn's restore would fight the operator). Raises
        ResidencyError (before evicting) when the model can't fit the box, so the caller
        refuses instead of crashing. Housekeeping hiccups are swallowed, like ensure_room."""
        if not self._enabled:
            return
        try:
            plan = await self._plan(served_model)
        except Exception as exc:  # noqa: BLE001 — housekeeping hiccup: best-effort, no-op
            log.warning("residency.free_room_failed", model=served_model, error=repr(exc))
            return
        if plan is None:
            return
        self._refuse_if_over_box(plan)  # raises before we evict anything
        self._displaced.discard(served_model)
        for served in plan.victims:
            with contextlib.suppress(LocalGatewayError):
                await self._gateway.unload(served)

    async def _restore(self) -> None:
        """Reload the displaced set that isn't already resident, as far as the budget allows
        (opportunistic — it never evicts to squeeze a member in; on-demand loads go through
        ensure_room). A member that fits is loaded and cleared from the displaced set; one
        that doesn't fit is left for a later restore, once whatever holds the RAM is gone. A
        load against a down/cold gateway is suppressed, so the worst case is a wasted probe,
        never a failed turn."""
        if not self._enabled:
            return
        targets = set(self._displaced)
        if not targets:
            return
        running = await self._gateway.running()
        mem = read_memory_gb()
        if mem is None:
            return  # can't budget the restore — leave cold members to load on demand
        total, used = mem
        ceiling = total * (1.0 - self._free_ram_fraction)
        windows = await self._windows()
        # Deterministic order when not everything fits: biggest footprint first, so we bring
        # back the model the turn was actually using before a smaller one. A bare set would
        # restore an arbitrary subset.
        scored: list[tuple[float, str]] = []
        for served in targets:
            scored.append((-await self._footprint(served, windows), served))
        scored.sort()
        for neg_fp, served in scored:
            fp = -neg_fp
            if served in running:
                self._displaced.discard(served)  # already back
                continue
            if used + fp > ceiling:
                continue  # no room without evicting a resident model — leave it for later
            with contextlib.suppress(LocalGatewayError):
                await self._gateway.load(served)
                used += fp  # bound the pass so several missing members don't over-commit
            # Attempted (loaded, or the gateway refused) → no longer pending. A transient miss
            # is re-displaced when it's next evicted, so we never spin retrying a since-removed
            # model.
            self._displaced.discard(served)
