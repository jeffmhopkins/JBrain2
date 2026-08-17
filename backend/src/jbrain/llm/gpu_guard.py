"""Watch the iGPU's device memory during a model load, and abort a load that runs away.

Why this exists, concretely: loading a Qwen3.8-27B variant that carried a vision projector
took this box's HOST down to a power cycle — twice — the second time with 105 GiB of free
system RAM and a model whose catalog footprint was 21 GiB. Memory pressure was not the
cause and the residency budget was never going to prevent it, because both the budget and
the estimate it checks are about SYSTEM RAM, and the allocation that killed the box was
GTT: system pages the amdgpu driver pins on the iGPU's behalf (llama.cpp #27146 — an
mmproj/mtmd model balloons GTT on an AMD iGPU under Vulkan).

The failure had three parts, and a durable fix needs all three:

  1. A load happened without the budget being consulted at all (llama-swap's `/upstream/`
     passthrough loads on demand). Closed at the source — `LocalGatewayClient.props`
     refuses a non-resident model.
  2. The predicted cost was a CATALOG ESTIMATE, and it was wrong by a lot. `GttBudget`
     below adds GTT headroom to the pre-flight check, so a load is refused when the device
     pool is short even though `MemAvailable` looks fine.
  3. Nothing watched the load while it ran. That is this module's job, and it is the part
     that matters most: an estimate can only be wrong in ways we have already seen, so the
     FIRST load of any new model is always a guess. The watchdog is what makes an unknown
     model safe, and it is the difference between "we tuned the estimate" and "this cannot
     take the box again."

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

import structlog

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


def refuse_if_no_device_room(sample: GpuMem | None, projected_gb: float, target: str) -> None:
    """Pre-flight: raise when the device pool cannot hold `projected_gb` on top of what is
    already pinned, keeping `MIN_FREE_GTT_GB` in reserve.

    This is the check the system-RAM budget cannot make. On an APU the two pools overlap —
    GTT is pinned system RAM — but they are accounted separately and drift apart: a model
    can be well inside the free-RAM floor while the device pool, capped by
    `amdgpu.gttsize`/`ttm.pages_limit`, has no room left. A None sample means we could not
    read the pool, so this is a no-op."""
    if sample is None:
        return
    headroom = sample.gtt_free_gb - MIN_FREE_GTT_GB
    if projected_gb > headroom:
        raise GpuBudgetError(
            f"{target} needs ~{projected_gb:.1f} GB of device memory but only "
            f"{headroom:.1f} GB is safely available (GTT {sample.gtt_used_gb:.1f}/"
            f"{sample.gtt_total_gb:.1f} GB used, holding {MIN_FREE_GTT_GB:.0f} GB back) — "
            "refusing to load rather than risk freezing the host."
        )


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
            if now.gtt_used_gb > ceiling_gb:
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
