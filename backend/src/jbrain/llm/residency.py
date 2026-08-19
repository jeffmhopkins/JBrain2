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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain import box_events
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import gpu_guard, local_catalog
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.llm.local_weights import weights_size_gb

log = structlog.get_logger()

# A box-wide critical section around evict+load. `ResidencyCoordinator` is a PER-PROCESS
# evictor, so the api and the worker each hold the free-RAM floor on their own — two loads
# in different processes can both read low memory, both skip eviction, and co-load past the
# floor (the deferred-video-vs-chat race). A cross-process lock serializes the load path so
# only one process evicts+loads at a time. Returns an async context manager; `None` (the
# default) means no locking — single-process/cloud/test callers keep the old behavior.
BoxLock = Callable[[], "contextlib.AbstractAsyncContextManager[None]"]

# A fixed, arbitrary advisory-lock key shared by every process on the box (the api and the
# worker), so `pg_advisory_xact_lock` serializes their model loads against each other.
# The single default for "how much RAM stays out of the model budget". Mirrors
# `Settings.local_llm_free_ram_fraction`; the operator override rides the settings
# store and is threaded in via `fraction_loader`.
DEFAULT_FREE_RAM_FRACTION = 0.15

_BOX_LOCK_KEY = 0x6A_42_52_41_4E_4C_4F_41  # "jBRANLOA"


def pg_box_lock(maker: async_sessionmaker[AsyncSession]) -> BoxLock:
    """A cross-process box lock backed by a Postgres transaction-level advisory lock — the
    one lock that spans the api and worker processes (an asyncio.Lock is per-process). The
    transaction-level variant auto-releases when the txn ends (even if the pooled connection
    is reused) and when the holding process dies, so a crash can never leak the lock. Held
    only across a model's evict+load, which is seconds and infrequent."""

    @contextlib.asynccontextmanager
    async def _lock() -> AsyncIterator[None]:
        async with maker() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOX_LOCK_KEY})
            yield

    return _lock


# Loads the live per-model context-window overrides (catalog id → tokens), so the memory
# budget sizes each model's KV against the window it actually serves.
WindowsLoader = Callable[[], Awaitable[Mapping[str, int]]]
# Reads the operator's live free-RAM floor override (fraction kept free), or None to use the
# construction-time config default. Called before every load so a settings-screen change
# takes effect with no restart; a read failure or junk value degrades to the config default.
FractionLoader = Callable[[], Awaitable[float | None]]
# Reads the served-model names code mode has reserved the box for (empty when code mode is
# off) — jcode's own executor + planner. While non-empty, `ensure_room` refuses to load any
# model NOT in the set (unless it's already resident) — code mode owns the box, so nothing
# evicts its models and no contending big model co-loads past physical RAM. Read before every
# load so toggling code mode takes effect with no restart; a read failure degrades to empty
# (not held), never blocking a load on a housekeeping hiccup.
HoldLoader = Callable[[], Awaitable[frozenset[str]]]
# Notified with a served name when this coordinator drops that model's primed KV prefix
# (an eviction, or a bare reload on restore). Synchronous and best-effort — see
# `ResidencyCoordinator._prefix_lost`.
PrefixLostHook = Callable[[str], None]


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
    # The TARGET's own footprint (GiB) — weights + KV, independent of what else is resident.
    # Distinct from `projected_gb` (the whole box after the load): the device-memory guard
    # needs the cost of this one model to judge whether its allocation is running away.
    # 0.0 when already resident (nothing is about to be allocated).
    target_gb: float
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
        slots_loader: WindowsLoader | None = None,
        models_dir: str = "",
        enabled: bool = False,
        # Matches `Settings.local_llm_free_ram_fraction` (0.15). It was 0.25, so any
        # construction path that did not pass the fraction explicitly reserved 30 GiB
        # instead of 18.2 on this box and planned against a ceiling 12 GiB tighter than
        # the one the guard and the meter use — one of eight disagreeing memory budgets
        # this repo carried (see docs/plans/MEMORY_ADMISSION_PLAN.md, D0).
        free_ram_fraction: float = DEFAULT_FREE_RAM_FRACTION,
        fraction_loader: FractionLoader | None = None,
        hold_loader: HoldLoader | None = None,
        auto_restore_loader: Callable[[], Awaitable[bool]] | None = None,
        box_lock: BoxLock | None = None,
        on_prefix_lost: PrefixLostHook | None = None,
        gpu_probe: gpu_guard.GpuMemProbe | None = None,
    ) -> None:
        self._gateway = gateway
        # Reads the iGPU's DEVICE memory (GTT) so a load is budgeted and watched against the
        # pool it actually allocates from, not just system RAM. None (tests, cloud-only, a box
        # with no amdgpu) keeps the prior unguarded behaviour — see `_guarded_load`.
        self._gpu_probe = gpu_probe
        # Inert on a cloud-only box (no local hosting): ensure_room/restore no-op and nothing
        # is ever recorded. Mirrors settings.local_llm_enabled.
        self._enabled = enabled
        self._windows_loader = windows_loader
        # Per-model llama-server slot counts (catalog id → -np). A second slot doubles the
        # model's KV, so the eviction budget must see it — sized like windows, read live.
        self._slots_loader = slots_loader
        self._models_dir = models_dir
        # The config-default floor. The live operator override (fraction_loader) wins over it
        # per load when wired and valid; this is the fallback when there's no override, no
        # loader, or the read fails — so the budget always has a floor.
        self._free_ram_fraction = free_ram_fraction
        self._fraction_loader = fraction_loader
        # When code mode holds the box, this loads the reserved coder's served name ("" when
        # code mode is off). While held, `ensure_room` refuses to load any other non-resident
        # model, so nothing evicts the coder or co-loads a second large model past physical RAM.
        self._hold_loader = hold_loader
        # The operator's end-of-turn RESTORE switch (Settings → LLM), read live so a flip
        # applies with no restart. Off means the box stops putting back what a displacement
        # took — models come back only when a turn actually needs them. It exists because a
        # restore is a model load the owner did not ask for at that moment, and while
        # diagnosing the box "nothing loads unless I say so" has to be reachable from the
        # PWA. Absent loader → on, the long-standing behaviour.
        self._auto_restore_loader = auto_restore_loader
        # Cross-process serialization of the evict+load path (pg_box_lock in production).
        # None → single-process behavior: evict only, and let the client trigger the load.
        # Set → hold the lock across evict AND the target load, so the loaded model's memory
        # is committed before release and a concurrent process's plan sees it (no co-load).
        self._box_lock = box_lock
        # Served names evicted (by us or by another displacement) and awaiting restore. The
        # box's remembered steady state minus whatever currently holds the RAM. Bounded by the
        # provisioned model count; entries clear as they reload or are attempted.
        self._displaced: set[str] = set()
        # Strong refs to the in-flight restore so the loop doesn't GC it mid-load (asyncio
        # holds only weak refs). At most one runs at a time — a fresh schedule while one is in
        # flight is dropped (the running one already restores the whole set), which coalesces a
        # multi-image turn's repeated displacements into a single end-of-turn restore.
        self._tasks: set[asyncio.Task[None]] = set()
        # Called with a served name whenever this coordinator does something that DROPS that
        # model's primed KV prefix — an eviction, or a bare reload during restore. WarmKeeper
        # registers it to clear its "already primed" memo.
        #
        # Without this the keeper only notices a lost prime when it happens to OBSERVE the
        # model missing from `running()` on a tick. An evict and its end-of-turn restore that
        # both land inside one 60s interval — an image render, a code-mode toggle — are
        # invisible to it: the memo stays set, the keeper reports settled, and the owner's next
        # jerv message pays the full cold prefill IN THE FOREGROUND. This hook is the missing
        # edge, and it is a callback rather than a direct call so residency keeps knowing
        # nothing about personas or priming.
        self._on_prefix_lost = on_prefix_lost

    def _prefix_lost(self, served_model: str) -> None:
        """Signal that `served_model`'s primed prefix is gone. Best-effort and synchronous —
        a listener that raises must never break an eviction."""
        if self._on_prefix_lost is None:
            return
        try:
            self._on_prefix_lost(served_model)
        except Exception:  # noqa: BLE001 — a notification hiccup is not an eviction failure
            log.warning("residency.prefix_lost_hook_failed", model=served_model, exc_info=True)

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
                self._prefix_lost(name)

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

    async def _auto_restore(self) -> bool:
        """The live end-of-turn restore switch. Defaults to ON when no loader is wired or the
        read fails: a settings-store hiccup must not silently leave the box refusing to drift
        back to its steady state."""
        if self._auto_restore_loader is None:
            return True
        with contextlib.suppress(Exception):
            return await self._auto_restore_loader()
        return True

    async def _windows(self) -> Mapping[str, int]:
        """Live per-model context-window overrides (catalog id → tokens); empty when no loader
        is wired or the read fails, so the budget falls back to catalog defaults."""
        if self._windows_loader is None:
            return {}
        with contextlib.suppress(Exception):
            return await self._windows_loader()
        return {}

    async def _slots(self) -> Mapping[str, int]:
        """Live per-model slot counts (catalog id → -np); empty when no loader is wired or the
        read fails, so the budget falls back to a single slot (no KV multiplier)."""
        if self._slots_loader is None:
            return {}
        with contextlib.suppress(Exception):
            return await self._slots_loader()
        return {}

    async def _fraction(self) -> float:
        """The live free-RAM floor (fraction kept free): the operator's stored override when
        wired and valid, else the construction-time config default. Read per-plan so a
        settings-screen change applies to the next load without a restart; a missing loader,
        a read failure, or a junk value (not in (0, 1)) all degrade to the config default —
        the budget must never lose its floor."""
        if self._fraction_loader is None:
            return self._free_ram_fraction
        with contextlib.suppress(Exception):
            override = await self._fraction_loader()
            if override is not None and 0.0 < override < 1.0:
                return override
        return self._free_ram_fraction

    async def _held_names(self) -> frozenset[str]:
        """The served-model names code mode has reserved the box for, or an empty set (not
        held). Read per-load so toggling code mode applies immediately; any hiccup degrades to
        empty — a housekeeping failure must never block a legitimate load (best-effort, like the
        rest of this coordinator)."""
        if self._hold_loader is None:
            return frozenset()
        with contextlib.suppress(Exception):
            names = await self._hold_loader()
            if names:
                return frozenset(names)
        return frozenset()

    async def _footprint(
        self, served_model: str, windows: Mapping[str, int], slots: Mapping[str, int]
    ) -> float:
        """A resident model's unified-memory footprint (GiB) at its served window and slot
        count — measured weights + KV (a second slot doubles the KV). 0.0 for a served name
        outside the catalog: we can't size it, so it never drives (or blocks) an eviction."""
        model = local_catalog.get_by_served(served_model)
        if model is None:
            return 0.0
        window = windows.get(model.id, model.context_window)
        n_slots = slots.get(model.id, 1)
        disk = weights_size_gb(self._models_dir, model.id) if self._models_dir else None
        return local_catalog.footprint_gb(model, window, disk_gb=disk, slots=n_slots)

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
        ceiling = total * (1.0 - await self._fraction())  # keep used at/under this
        windows = await self._windows()
        slots = await self._slots()
        if served_model in running:
            return EvictionPlan(
                target=served_model,
                victims=(),
                resident_gb=used,
                projected_gb=used,
                target_gb=0.0,  # already resident: nothing more gets allocated
                ceiling_gb=ceiling,
                total_gb=total,
                fits=True,
                over=False,
                over_box=False,
                already_resident=True,
            )
        target_gb = await self._footprint(served_model, windows, slots)
        predicted = used + target_gb
        if predicted <= ceiling:  # fits alongside what's resident — evict nothing
            return EvictionPlan(
                target=served_model,
                victims=(),
                resident_gb=used,
                projected_gb=predicted,
                target_gb=target_gb,
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
            ranked.append((-await self._footprint(served, windows, slots), served))
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
            target_gb=target_gb,
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
        swallowed (housekeeping never fails a turn); the deliberate over-box refusal is not.

        With a `box_lock` (production), the evict AND the target load run inside a
        cross-process lock, so two processes can't both read low memory, skip eviction, and
        co-load past the floor. Without one, this is the original per-process evict-only path
        (the client triggers the load), so single-process/cloud/test callers are unchanged."""
        if not self._enabled:
            return
        # Code-mode exclusivity: while the box is reserved for code mode, refuse to load ANY
        # model outside its reserved set (jcode's executor + planner). A model already resident
        # may keep serving (no new load, no memory pressure), but a NON-resident, non-reserved
        # model is refused — so nothing evicts code mode's models and no second large model
        # co-loads past physical RAM (the unified-memory OOM this guards). The reserved models
        # are exempt (they must be able to (re)load). Checked before the box lock so a refused
        # load never contends for it.
        held = await self._held_names()
        if held and served_model not in held:
            with contextlib.suppress(Exception):
                if served_model in await self._gateway.running():
                    return  # already resident — serving it needs no load
            raise ResidencyError(
                f"Code mode is holding the box for {sorted(held)}. Turn code mode off to run "
                "other models (chat, vision, or background research)."
            )
        if self._box_lock is None:
            await self._ensure_room_core(served_model, load_target=False)
            return
        # Fast path, lock-free: already resident → no evict, no load, no race to serialize.
        with contextlib.suppress(Exception):
            if served_model in await self._gateway.running():
                self._displaced.discard(served_model)
                return
        # Slow path: a load is needed — serialize evict+load box-wide and load the target
        # under the lock so its memory is committed before the next process plans.
        async with self._box_locked():
            await self._ensure_room_core(served_model, load_target=True)

    async def _ensure_room_core(self, served_model: str, *, load_target: bool) -> None:
        """The evict (and, under the box lock, load) work. `load_target` loads+warms the
        target after evicting so a cross-process holder sees it resident before the lock
        releases; off, the client triggers the load lazily (the un-serialized path)."""
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
        # The eviction is narrated with WHO it was for: on the vitals surface "unloaded
        # qwen35 — to make room for gpt-oss-120b" is the line that turns two unexplained
        # GPU events into one comprehensible swap.
        with box_events.because(f"to make room for {served_model}"):
            for served in plan.victims:
                with contextlib.suppress(LocalGatewayError):
                    await self._gateway.unload(served)
                    self._displaced.add(served)  # remember it for the end-of-turn restore
                    self._prefix_lost(served)
        if load_target and not plan.already_resident:
            await self._guarded_load(served_model, plan.target_gb)

    async def _guarded_load(self, served_model: str, projected_gb: float) -> None:
        """Load, and log what the load ACTUALLY cost in device memory.

        The device-memory guard itself no longer lives here. It moved into
        `LocalGatewayClient.load`, which is the single chokepoint every path to committing
        GPU memory passes through — this coordinator was only one of six callers, and the
        three freezes this box took all came in through callers that never reached this
        method (the settings screen's deliberate load, the debug console's, and this
        coordinator's own end-of-turn RESTORE). Guarding the wrapper protected the one
        caller that used the wrapper; guarding the chokepoint protects all of them, and
        leaves no way to opt out.

        `projected_gb` stays a parameter because the measurement below is worth keying to the
        plan's estimate: the delta between predicted and measured is how the catalog numbers
        get corrected from data rather than from the next freeze."""
        probe = self._gpu_probe
        baseline = await probe.sample() if probe is not None else None
        # A gateway failure stays non-fatal — the completion that wanted the model fails on its
        # own, and a housekeeping restore must not take a turn down with it. But it is LOGGED,
        # and it suppresses the measurement below: `load_measured` on a load that never
        # happened reads as a successful load whose footprint came in near zero, which is
        # exactly backwards from the truth and poisons the predicted-vs-measured series the
        # catalog numbers are corrected from. (A GpuBudgetError is deliberately NOT caught here
        # — a refusal is the operator's business and propagates.)
        try:
            await self._gateway.load(served_model)
        except LocalGatewayError as exc:
            log.warning("residency.load_failed", model=served_model, error=repr(exc))
            return
        if probe is not None:
            measured = await gpu_guard.measure_footprint(probe, baseline, served_model)
            if measured is not None:
                log.info(
                    "residency.load_measured",
                    model=served_model,
                    predicted_gb=round(projected_gb, 2),
                    measured_gb=round(measured, 2),
                )

    @contextlib.asynccontextmanager
    async def _box_locked(self) -> AsyncIterator[None]:
        """Hold the cross-process box lock across the block, best-effort: if acquiring it
        fails (DB down / lock infra hiccup), degrade to running UNLOCKED rather than fail the
        turn — the free-RAM budget still applies, we just lose cross-process serialization
        for that one load (the OS backstop, earlyoom, covers the residual)."""
        assert self._box_lock is not None
        cm: contextlib.AbstractAsyncContextManager[None] | None = None
        try:
            cm = self._box_lock()
            await cm.__aenter__()
        except Exception as exc:  # noqa: BLE001 — lock is best-effort; proceed unlocked
            log.warning("residency.box_lock_unavailable", error=repr(exc))
            cm = None
        try:
            yield
        finally:
            if cm is not None:
                with contextlib.suppress(Exception):
                    await cm.__aexit__(None, None, None)

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
        with box_events.because(f"to make room for {served_model}, which you loaded"):
            for served in plan.victims:
                with contextlib.suppress(LocalGatewayError):
                    await self._gateway.unload(served)
                    self._prefix_lost(served)

    async def _restore(self) -> None:
        """Reload the displaced set that isn't already resident, as far as the budget allows
        (opportunistic — it never evicts to squeeze a member in; on-demand loads go through
        ensure_room). A member that fits is loaded and cleared from the displaced set; one
        that doesn't fit is left for a later restore, once whatever holds the RAM is gone. A
        load against a down/cold gateway is suppressed, so the worst case is a wasted probe,
        never a failed turn."""
        if not self._enabled:
            return
        if not await self._auto_restore():
            log.info("residency.restore_disabled", displaced=sorted(self._displaced))
            return
        # While code mode holds the box, do NOT opportunistically reload displaced members — a
        # restore load bypasses ensure_room's refusal, so this is where a stray model could
        # slip in beside code mode's own. Skip; the members stay displaced and restore once the
        # hold clears (jcode OFF clears the flag before it fires its own restore, so that path
        # is unaffected).
        if await self._held_names():
            return
        targets = set(self._displaced)
        if not targets:
            return
        running = await self._gateway.running()
        mem = read_memory_gb()
        if mem is None:
            return  # can't budget the restore — leave cold members to load on demand
        total, used = mem
        ceiling = total * (1.0 - await self._fraction())
        windows = await self._windows()
        slots = await self._slots()
        # Deterministic order when not everything fits: biggest footprint first, so we bring
        # back the model the turn was actually using before a smaller one. A bare set would
        # restore an arbitrary subset.
        scored: list[tuple[float, str]] = []
        for served in targets:
            scored.append((-await self._footprint(served, windows, slots), served))
        scored.sort()
        for neg_fp, served in scored:
            fp = -neg_fp
            if served in running:
                self._displaced.discard(served)  # already back
                continue
            if used + fp > ceiling:
                continue  # no room without evicting a resident model — leave it for later
            with (
                contextlib.suppress(LocalGatewayError, gpu_guard.GpuBudgetError),
                # A restore is a load nobody asked for at that moment, so it is the one most
                # likely to read as the box misbehaving. Say whose it is.
                box_events.because("putting back what a displacement took"),
            ):
                # A BARE load: weights back and the inference path warm, but no persona/tools
                # prefill — so the model is resident and UNPRIMED. Say so, or the keeper's memo
                # (set before the eviction) makes it skip the re-prime the model now needs.
                await self._gateway.load(served)
                self._prefix_lost(served)
                used += fp  # bound the pass so several missing members don't over-commit
            # Attempted (loaded, or the gateway refused) → no longer pending. A transient miss
            # is re-displaced when it's next evicted, so we never spin retrying a since-removed
            # model.
            self._displaced.discard(served)
