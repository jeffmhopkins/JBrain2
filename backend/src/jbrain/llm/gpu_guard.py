"""Watch the iGPU's device memory during a model load, and abort a load that runs away.

Why this exists: this box's HOST went down to a power cycle three times while loading local
models — one of them with 105 GiB of free system RAM. A clean OOM kill would have cost a
process; instead the machine stopped answering.

WHAT ACTUALLY CAUSES THAT, from v6.18 kernel source, because the first explanation written
here was wrong and it is worth being precise:

  - GTT pages (system pages the amdgpu driver pins for the iGPU) are allocated `GFP_HIGHUSER`
    — unmovable, on no LRU, NOT reclaimable.
  - TTM registers no shrinker for live BO pages, only for its free-page cache. The kernel
    cannot reclaim a loaded model's GTT under any amount of pressure.
  - `amdgpu_bo_create()` sets `.gfp_retry_mayfail = true` ("We opt to avoid OOM on system
    pages allocations"), and per the kernel's memory-allocation guide that flag means THE OOM
    KILLER IS NOT CALLED.

So exhausting RAM through GTT puts every task into direct reclaim with nothing to reclaim and
no escalation path. Nothing dies. That is the freeze. The host-level guard against it is
`ttm.pages_limit`, which turns an over-commit into a clean `-ENOSPC` before the pages are
requested — and this box currently sets it to ~100% of RAM, which disables that boundary. See
docs/runbooks/STRIX_HALO_SETUP.md; fixing it is a host step, not something this module can do.

What this module CAN do is make the software side incapable of walking into it:

  1. A load happened without the budget being consulted at all (llama-swap's `/upstream/`
     passthrough loads on demand). Closed at the source — `LocalGatewayClient.props`
     refuses a non-resident model.
  2. The predicted cost was a CATALOG ESTIMATE, and it was wrong. `refuse_if_no_device_room`
     checks the pre-flight against the DEVICE pool rather than only system RAM. (The estimate
     itself is fixed in `local_catalog`: the KV term was reading a q4_0 figure for an f16
     cache, and the runtime terms were missing entirely.)
  3. Nothing watched the load while it ran. That is this module's job, and it is the part
     that matters most: an estimate can only be wrong in ways we have already seen, so the
     FIRST load of any new model is always a guess. The watchdog is what makes an unknown
     model safe, and it is the difference between "we tuned the estimate" and "this cannot
     take the box again."
  4. The check sat on a WRAPPER, not on the path. The residency coordinator guarded its own
     loads and was one of six callers; the three freezes all arrived through the other five
     (the settings screen's load, the debug console's, the coordinator's own end-of-turn
     restore). So the guard now lives inside `LocalGatewayClient.load` — the single
     chokepoint to committing device memory — and there is no longer an unguarded way in.

⚠️ KNOWN LIMITS OF THIS GUARD, so nobody mistakes it for a proof:
  - `mem_info_gtt_used` is DEVICE-WIDE, not per-process. Per-model attribution needs
    `/proc/<pid>/fdinfo/<drm-fd>` → `drm-resident-gtt` (kernel ≥ 6.14), never `drm-total-gtt`
    (which is keyed by PREFERRED placement, so on this APU a GTT-resident buffer is filed
    under vram).
  - GTT does NOT add to `MemAvailable` — GTT pages come from the buddy allocator, so
    `si_mem_available()` already reflects them 1:1. Subtracting GTT from a RAM figure anywhere
    would double-reserve by the size of every loaded model. The correct form is
    `min(gtt_total - gtt_used, MemAvailable - reserve)`.
  - Sampling GTT once a second is a weaker livelock detector than PSI. `/proc/pressure/memory`
    with a `full` trigger fires within ~50 ms on precisely the stall-without-OOM state amdgpu
    creates. Worth moving to; not done here.

The numbers come from the supervisor (`/metrics` → `gpu_mem`), which reads
`/sys/class/drm/card*/device/mem_info_{gtt,vram}_{used,total}`. The api container does not
mount `/sys`, and the supervisor is this deployment's single owner of host access, so that
is the seam rather than a new bind mount.

Best-effort by contract, with ONE deliberate exception: a probe that cannot read the device
pool degrades to today's behaviour (system-RAM budget only, no watchdog) rather than
blocking loads on a box with no amdgpu — but a probe that CAN read it and sees the ceiling
crossed aborts the load and raises. Silence is the fallback for "we don't know"; it is
never the response to "we know this is going wrong."
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from jbrain.host_metrics import read_memory_gb

log = structlog.get_logger()

_BYTES_PER_GB = 1024**3

# How often the watchdog samples the device pool while a load runs. A model load is tens of
# seconds of I/O, so a 1s sample gives many chances to catch a climb; polling faster buys
# little and costs a supervisor round-trip each time.
SAMPLE_INTERVAL_S = 1.0

# How far past its predicted footprint a load may push GTT before it is judged a runaway.
# Generous on purpose: llama.cpp's real device usage exceeds the weights (compute buffers,
# the graph, alignment), so a tight multiple would abort healthy loads. What it catches is
# the ORDER-OF-MAGNITUDE balloon that takes the host, not ordinary overshoot.
RUNAWAY_MULTIPLE = 1.75

# GTT the box must still have free for the host to stay alive. The freeze mode here is a
# reclaim livelock, not a clean OOM kill — the machine stops answering rather than losing a
# process — so this is a hard floor, held even when a load's own prediction says it fits.
MIN_FREE_GTT_GB = 6.0


@dataclass(frozen=True)
class GpuMem:
    """A device-memory sample, in GB. `gtt_*` is the pool that matters on an APU: with no
    dedicated VRAM, a loaded model's device buffers are GTT, pinned out of the same system
    RAM the free-RAM budget totals — which is exactly why counting only one of them missed
    a freeze."""

    gtt_used_gb: float
    gtt_total_gb: float
    vram_used_gb: float
    vram_total_gb: float

    @property
    def gtt_free_gb(self) -> float:
        return max(0.0, self.gtt_total_gb - self.gtt_used_gb)

    @property
    def device_used_gb(self) -> float:
        """Everything the GPU holds. On a small-carveout APU this is ~all GTT, but a box
        configured with a large fixed UMA carveout puts real bytes in VRAM too."""
        return self.gtt_used_gb + self.vram_used_gb


class GpuMemProbe(Protocol):
    """Reads the current device-memory sample, or None when it can't be determined (no
    amdgpu, supervisor unreachable, a build that stops exposing the counters). None is a
    'we don't know' signal and must degrade to the prior behaviour, never to a refusal."""

    async def sample(self) -> GpuMem | None: ...


class SupervisorGpuMemProbe:
    """Reads `gpu_mem` from the supervisor's `/metrics`.

    The supervisor already collects these counters for the Ops screen — the values were
    being measured, shipped over the wire and DRAWN FOR THE OWNER while the code that
    decides whether a load is safe never looked at them. This closes that gap rather than
    adding new plumbing.

    Takes a client FACTORY, not a client. The residency coordinator is constructed early in
    startup, before `app.state.supervisor_client` exists, so resolving the client eagerly
    binds an attribute that isn't there yet — the same late-binding the `on_prefix_lost` hook
    needs, for the same reason. A factory that returns None (not wired yet, or not wired at
    all) degrades to an unmeasurable pool rather than raising."""

    def __init__(self, client_factory: Callable[[], object | None], token: str = "") -> None:
        self._client_factory = client_factory
        self._token = token

    async def sample(self) -> GpuMem | None:
        client = self._client_factory()
        if client is None:
            return None
        try:
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
            resp = await client.get("/metrics", headers=headers)  # type: ignore[attr-defined]
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — an unreadable probe degrades, never blocks
            log.info("gpu_guard.probe_unavailable", error=str(exc))
            return None
        return parse_gpu_mem(body)


class _OwnClientProbe:
    """A probe that opens its own supervisor connection per sample.

    For processes with no long-lived supervisor client to borrow: the CLI (`jbrain smoketest`
    loads every installed model during an UPDATE — a load path, therefore a freeze path) and
    the router's default residency coordinator. A short-lived connection per sample costs one
    handshake a second during a load, which is nothing next to what it is guarding against.
    Inert without a token, matching every other supervisor caller."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url
        self._token = token

    async def sample(self) -> GpuMem | None:
        if not (self._base_url and self._token):
            return None
        try:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0) as client:
                resp = await client.get(
                    "/metrics", headers={"Authorization": f"Bearer {self._token}"}
                )
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:  # noqa: BLE001 — an unreadable probe degrades, never blocks
            log.info("gpu_guard.probe_unavailable", error=str(exc))
            return None
        return parse_gpu_mem(body)


def probe_for(settings: object) -> GpuMemProbe:
    """The device-memory probe for a process that has no supervisor client of its own.

    Takes the app settings duck-typed rather than by import, so `gpu_guard` stays a leaf of
    the llm package (the gateway imports it, and the config module imports plenty)."""
    return _OwnClientProbe(
        str(getattr(settings, "supervisor_url", "") or ""),
        str(getattr(settings, "supervisor_token", "") or ""),
    )


def parse_gpu_mem(body: object) -> GpuMem | None:
    """The supervisor's `/metrics` body → a sample, or None when it carries no `gpu_mem`
    (a non-AMD box, or a supervisor too old to report it). Tolerant by design: this must
    not start refusing loads because a field moved."""
    if not isinstance(body, dict):
        return None
    raw = body.get("gpu_mem")
    if not isinstance(raw, dict):
        return None
    try:
        return GpuMem(
            gtt_used_gb=float(raw["gtt_used_bytes"]) / _BYTES_PER_GB,
            gtt_total_gb=float(raw["gtt_total_bytes"]) / _BYTES_PER_GB,
            vram_used_gb=float(raw.get("vram_used_bytes") or 0) / _BYTES_PER_GB,
            vram_total_gb=float(raw.get("vram_total_bytes") or 0) / _BYTES_PER_GB,
        )
    except (KeyError, TypeError, ValueError):
        return None


class GpuBudgetError(Exception):
    """A load was refused or aborted on device-memory grounds. Distinct from a housekeeping
    hiccup so callers surface it instead of swallowing it — the operator needs to know the
    box declined to do something, not silently get a model that never loaded."""


def refuse_if_no_device_room(
    sample: GpuMem | None,
    projected_gb: float,
    target: str,
    *,
    host_free_gb: float | None = None,
) -> None:
    """Pre-flight: raise when the box cannot hold `projected_gb` on top of what is already
    pinned, keeping `MIN_FREE_GTT_GB` in reserve.

    Bounded by BOTH pools, and by the smaller of the two:

    * the device ceiling — `gtt_total - gtt_used`, capped by `amdgpu.gttsize`/`ttm.pages_limit`;
    * the host's actually-free pages — `host_metrics.read_memory_gb`.

    Taking only the first is what let this box die on 2026-08-19. `strix-halo-host-setup.sh`
    sets `amdgpu.gttsize=126976` (124 GiB) on a 121 GiB machine, so `gtt_total` is ESSENTIALLY
    ALL OF RAM and `gtt_free` therefore counts page cache, Postgres and every container's
    anonymous memory as room the GPU could take. At the moment of the fatal load the device
    ceiling reported 50.4 GB of headroom while the box had 8.0 GB of free pages: the guard
    admitted a 27 GB model into 8 GB, and the reclaim that followed livelocked the host for
    seven hours. The device number was not wrong about the ceiling — it was answering a
    different question from the one that kills the machine.

    A None sample (or an unreadable host figure) drops that term rather than the whole check,
    so losing one probe degrades the guard instead of disabling it. Both None is a no-op, as
    before — with nothing to read, refusing every load would be worse than the risk."""
    device_headroom = None if sample is None else sample.gtt_free_gb - MIN_FREE_GTT_GB
    if host_free_gb is None:
        host_free_gb = _host_free_gb()
    host_headroom = None if host_free_gb is None else host_free_gb - MIN_FREE_GTT_GB

    limits = [
        (h, name)
        for h, name in ((device_headroom, "GTT"), (host_headroom, "host"))
        if h is not None
    ]
    if not limits:
        return
    headroom, binding = min(limits)
    if projected_gb <= headroom:
        return

    where = (
        f"GTT {sample.gtt_used_gb:.1f}/{sample.gtt_total_gb:.1f} GB used"
        if sample is not None
        else "GTT unreadable"
    )
    free = (
        f"{host_free_gb:.1f} GB host pages free" if host_free_gb is not None else "host unreadable"
    )
    raise GpuBudgetError(
        f"{target} needs ~{projected_gb:.1f} GB but only {headroom:.1f} GB is safely "
        f"available — bound by the {binding} limit ({where}, {free}, holding "
        f"{MIN_FREE_GTT_GB:.0f} GB back) — refusing to load rather than risk freezing the host."
    )


def _host_free_gb() -> float | None:
    """Free host pages, or None when `/proc/meminfo` cannot be read. Separate from the device
    probe on purpose — the two answer different questions, and on this hardware only this one
    moves before the machine stops answering."""
    mem = read_memory_gb()
    return None if mem is None else mem[0] - mem[1]


async def guarded_load(
    load: Callable[[], Awaitable[None]],
    *,
    probe: GpuMemProbe,
    projected_gb: float,
    target: str,
    abort: Callable[[], Awaitable[None]],
    sample_interval_s: float = SAMPLE_INTERVAL_S,
) -> None:
    """Run `load()` while watching device memory, and abort it if GTT runs away.

    The load is cancelled and `abort()` is called (which unloads the half-loaded model) when
    either the climb exceeds `RUNAWAY_MULTIPLE` × its predicted footprint, or free GTT falls
    below `MIN_FREE_GTT_GB`. Then it raises `GpuBudgetError`.

    This is the guard for a model NOBODY HAS CHARACTERIZED. A better estimate only protects
    against costs we already know; the first load of anything new is a guess, and on this
    hardware a wrong guess does not fail the load, it takes the machine. Watching the actual
    number while it climbs is the only check that works on the first attempt.

    THIS IS A WATCHDOG, NOT A PROGRESS BAR, and it briefly tried to be both. Device memory
    here is the RESERVATION: llama.cpp commits the whole GTT buffer before it reads a byte
    (measured, `local_gateway._sweep_page_cache_during_load` — 57 GB committed up front), so
    the fraction it yields is already at 0.78 four seconds into a load that has three more
    minutes to run. That reading is exactly right for catching a runaway, which is a question
    about the booking, and wrong for "how far in is this", which is a question about the read.
    The bar is fed from the page-cache sweep and the warm-up's prefill instead.

    Degrades cleanly: if the probe can't read the pool (no amdgpu, supervisor down), the
    load runs exactly as it does today, unwatched — a box that can't measure must still be
    able to serve. Aborting is best-effort too, and it is deliberately attempted before the
    raise: on a runaway the priority is getting the allocation released, not a tidy error."""
    baseline = await probe.sample()
    if baseline is None:
        log.info("gpu_guard.unwatched_load", model=target, reason="no device-memory probe")
        await load()
        return

    ceiling_gb = baseline.gtt_used_gb + max(projected_gb * RUNAWAY_MULTIPLE, projected_gb + 2.0)
    task = asyncio.ensure_future(load())
    breach: str | None = None
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=sample_interval_s)
            if done:
                break
            now = await probe.sample()
            if now is None:
                continue  # lost the probe mid-load: fall back to running unwatched
            host_free = _host_free_gb()
            if host_free is not None and host_free < MIN_FREE_GTT_GB:
                # THE FLOOR THAT CAN ACTUALLY FIRE ON THIS BOX. `gtt_free` is not a second
                # opinion here: `strix-halo-host-setup.sh` sets `amdgpu.gttsize` to 124 GiB on a
                # 121 GiB machine, so the device pool is essentially all of RAM and `gtt_used`
                # would have to pass ~118 GiB — which the pre-flight already refuses — before
                # the check below trips. `refuse_if_no_device_room` was given the host term
                # after the 2026-08-19 livelock, where the device ceiling reported 50.4 GB of
                # headroom on a box with 8.0 GB of free pages; the WATCHDOG never was, so the
                # guard that is supposed to protect the first load of an uncharacterised model
                # was watching the one number that cannot move.
                breach = (
                    f"free host memory fell to {host_free:.1f} GB while loading {target} "
                    f"(floor {MIN_FREE_GTT_GB:.0f} GB) — the reclaim livelock this box hangs "
                    "on starts here, not at the GTT cap"
                )
            elif now.gtt_used_gb > ceiling_gb:
                breach = (
                    f"device memory ran away while loading {target}: GTT "
                    f"{now.gtt_used_gb:.1f} GB, past the {ceiling_gb:.1f} GB ceiling for a "
                    f"model predicted at {projected_gb:.1f} GB"
                )
            elif now.gtt_free_gb < MIN_FREE_GTT_GB:
                breach = (
                    f"free device memory fell to {now.gtt_free_gb:.1f} GB while loading "
                    f"{target} (floor {MIN_FREE_GTT_GB:.0f} GB)"
                )
            if breach:
                log.error("gpu_guard.aborting_load", model=target, reason=breach)
                task.cancel()
                break
        if breach is None:
            await task  # completed on its own — surface its own error, if any
            # A final check AFTER the load returns. The in-flight loop only sees what is
            # allocated while it is still running, and a load can report success with the
            # device allocation still settling — or finish between two samples, which is the
            # normal case for a fast load. Without this the guard would have a hole exactly
            # the width of its sampling interval.
            settled = await probe.sample()
            if settled is not None:
                if settled.gtt_used_gb > ceiling_gb:
                    breach = (
                        f"{target} finished loading at GTT {settled.gtt_used_gb:.1f} GB, past "
                        f"the {ceiling_gb:.1f} GB ceiling for a model predicted at "
                        f"{projected_gb:.1f} GB"
                    )
                elif settled.gtt_free_gb < MIN_FREE_GTT_GB:
                    breach = (
                        f"{target} finished loading with only {settled.gtt_free_gb:.1f} GB of "
                        f"device memory free (floor {MIN_FREE_GTT_GB:.0f} GB)"
                    )
                if breach:
                    log.error("gpu_guard.unloading_after_load", model=target, reason=breach)
    finally:
        if breach is None and not task.done():
            # THE CALLER WAS CANCELLED (shutdown, a jcode swap, an enclosing timeout). Without
            # this the load runs on, orphaned: no sampling — the watchdog loop above is gone —
            # while `load()`'s own `finally` has already released the per-process load lock, so
            # a second load can start beside it. That was survivable while the orphan was only
            # the weights read; since the warm moved inside, the orphan is the whole load. Kill
            # it and unload, for the same reason a breach does.
            log.warning("gpu_guard.cancelling_orphaned_load", model=target)
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            with contextlib.suppress(Exception):
                await abort()
        if breach is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Release first, explain second: the box is mid-runaway and the allocation
            # matters more than the exception.
            with contextlib.suppress(Exception):
                await abort()
    if breach is not None:
        raise GpuBudgetError(
            f"{breach}. The load was aborted and the model unloaded — the host was at risk "
            "of a hard freeze, which on this hardware needs a power cycle."
        )


async def measure_footprint(probe: GpuMemProbe, before: GpuMem | None, target: str) -> float | None:
    """Device memory a just-completed load actually pinned, in GB — the measurement that
    turns a catalog guess into a fact. None when either sample is missing.

    Logged rather than stored for now: persisting it (keyed by model + window + slots, so
    the budget uses the measured number instead of `size_gb + kv_gb`) is the natural next
    step, and this is the reading it needs."""
    if before is None:
        return None
    after = await probe.sample()
    if after is None:
        return None
    delta = after.device_used_gb - before.device_used_gb
    log.info("gpu_guard.measured_footprint", model=target, device_gb=round(delta, 2))
    return delta
