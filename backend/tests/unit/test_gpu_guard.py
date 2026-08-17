"""The device-memory guard: the pre-flight GTT check and the load watchdog.

These cover the failure that took the host down twice — a model load whose real device
(GTT) cost far exceeded its catalog footprint, on a box with plenty of free system RAM.
The system-RAM budget could not see it; every test here is about the check that can.
"""

import asyncio
from typing import Any

import pytest

from jbrain.llm import gpu_guard
from jbrain.llm.gpu_guard import GpuBudgetError, GpuMem

_GB = 1024**3


def _sample(gtt_used: float, gtt_total: float = 120.0, vram_used: float = 0.5) -> GpuMem:
    return GpuMem(
        gtt_used_gb=gtt_used, gtt_total_gb=gtt_total, vram_used_gb=vram_used, vram_total_gb=2.0
    )


class _ScriptedProbe:
    """Replays a fixed series of samples, holding the last one once exhausted."""

    def __init__(self, samples: list[GpuMem | None]) -> None:
        self._samples = list(samples)
        self.calls = 0

    async def sample(self) -> GpuMem | None:
        self.calls += 1
        if not self._samples:
            return None
        return self._samples.pop(0) if len(self._samples) > 1 else self._samples[0]


# --- parsing ---------------------------------------------------------------


def test_parse_reads_the_supervisors_gpu_mem_block() -> None:
    # The supervisor already ships these counters for the Ops screen; the guard reads the
    # same body rather than adding plumbing.
    body = {
        "mem_total_bytes": 130 * _GB,
        "gpu_mem": {
            "gtt_used_bytes": int(68.0 * _GB),
            "gtt_total_bytes": int(120.0 * _GB),
            "vram_used_bytes": int(0.5 * _GB),
            "vram_total_bytes": int(2.0 * _GB),
        },
    }
    got = gpu_guard.parse_gpu_mem(body)
    assert got is not None
    assert got.gtt_used_gb == pytest.approx(68.0, abs=0.01)
    assert got.gtt_free_gb == pytest.approx(52.0, abs=0.01)
    assert got.device_used_gb == pytest.approx(68.5, abs=0.01)


def test_parse_returns_none_rather_than_guessing() -> None:
    # A non-AMD box, an older supervisor, or a moved field must read as "unknown" — which
    # degrades to the prior behaviour. It must never look like "zero used", which would
    # make the guard cheerfully approve everything.
    assert gpu_guard.parse_gpu_mem({}) is None
    assert gpu_guard.parse_gpu_mem({"gpu_mem": None}) is None
    assert gpu_guard.parse_gpu_mem({"gpu_mem": {"gtt_used_bytes": "?"}}) is None
    assert gpu_guard.parse_gpu_mem("nonsense") is None


# --- pre-flight ------------------------------------------------------------


def test_preflight_refuses_when_the_device_pool_is_short() -> None:
    # THE case the free-RAM budget cannot make. GTT is capped by amdgpu.gttsize /
    # ttm.pages_limit, so the device pool can be exhausted while MemAvailable still looks
    # roomy — the two are accounted separately and drift apart.
    with pytest.raises(GpuBudgetError) as exc:
        gpu_guard.refuse_if_no_device_room(_sample(gtt_used=110.0), projected_gb=21.0, target="m")
    assert "device memory" in str(exc.value)
    assert "freezing the host" in str(exc.value)


def test_preflight_holds_a_floor_back_for_the_host() -> None:
    # The freeze mode is a reclaim livelock, not a clean OOM kill — the machine stops
    # answering rather than losing a process — so a load that would fit exactly is still
    # refused. 10 GB free, 6 GB floor: a 5 GB model fits, a 6 GB one does not.
    sample = _sample(gtt_used=110.0, gtt_total=120.0)
    gpu_guard.refuse_if_no_device_room(sample, projected_gb=3.9, target="m")  # fits
    with pytest.raises(GpuBudgetError):
        gpu_guard.refuse_if_no_device_room(sample, projected_gb=4.1, target="m")


def test_preflight_is_inert_without_a_reading() -> None:
    # A box with no amdgpu (or an unreachable supervisor) must still be able to serve. An
    # unmeasurable pool degrades to today's behaviour, never to a refusal.
    gpu_guard.refuse_if_no_device_room(None, projected_gb=999.0, target="m")


# --- the watchdog ----------------------------------------------------------


async def test_watchdog_aborts_the_runaway_that_froze_the_box() -> None:
    # The regression this module exists for, in miniature: a model predicted at 21 GB whose
    # device allocation climbs without bound. Before this guard the load ran to completion
    # and took the HOST with it — a power cycle, twice. Now it is cancelled, the half-loaded
    # model is unloaded, and the caller is told.
    probe = _ScriptedProbe([_sample(1.0), _sample(20.0), _sample(45.0), _sample(90.0)])
    aborted: list[str] = []
    finished = False

    async def load() -> None:
        nonlocal finished
        await asyncio.sleep(5)  # a load that would have run to completion
        finished = True

    async def abort() -> None:
        aborted.append("unloaded")

    with pytest.raises(GpuBudgetError) as exc:
        await gpu_guard.guarded_load(
            load,
            probe=probe,
            projected_gb=21.0,
            target="qwen3.8-27b-mtp",
            abort=abort,
            sample_interval_s=0.01,
        )
    assert "ran away" in str(exc.value)
    assert aborted == ["unloaded"], "the half-loaded model must be released"
    assert not finished, "the load must be cancelled, not merely reported on"


async def test_watchdog_aborts_when_free_device_memory_hits_the_floor() -> None:
    # The second trip-wire, isolated: this load stays UNDER its own runaway ceiling
    # (100 baseline + 17.5 for a 10 GB model = 117.5) and is still aborted, because free GTT
    # falls to 4 GB against a 6 GB floor. A load can behave itself and still starve the box,
    # since other things — another model, an image render — hold GTT too. The floor is absolute.
    probe = _ScriptedProbe([_sample(100.0, gtt_total=120.0), _sample(116.0, gtt_total=120.0)])
    aborted: list[str] = []

    async def load() -> None:
        await asyncio.sleep(5)

    async def abort() -> None:
        aborted.append("unloaded")

    with pytest.raises(GpuBudgetError) as exc:
        await gpu_guard.guarded_load(
            load,
            probe=probe,
            projected_gb=10.0,
            target="m",
            abort=abort,
            sample_interval_s=0.01,
        )
    assert "free device memory fell" in str(exc.value)
    assert aborted == ["unloaded"]


async def test_watchdog_lets_a_healthy_load_finish_untouched() -> None:
    # Real loads exceed their weight size (compute buffers, the graph, alignment), so the
    # ceiling is deliberately generous. A load that grows to roughly its footprint must not
    # be aborted — a guard that fires on healthy loads gets turned off, and then protects
    # nothing.
    probe = _ScriptedProbe([_sample(1.0), _sample(12.0), _sample(22.0)])
    finished = False
    aborted: list[str] = []

    async def load() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    async def abort() -> None:
        aborted.append("unloaded")

    await gpu_guard.guarded_load(
        load, probe=probe, projected_gb=21.0, target="m", abort=abort, sample_interval_s=0.01
    )
    assert finished and aborted == []


async def test_watchdog_runs_the_load_unwatched_when_it_cannot_measure() -> None:
    # No amdgpu, or a supervisor that can't answer: the box must still serve. Degrading to
    # today's (unwatched) behaviour is correct; refusing to load would make an unmeasurable
    # box useless.
    probe = _ScriptedProbe([None])
    finished = False

    async def load() -> None:
        nonlocal finished
        finished = True

    async def abort() -> None:
        raise AssertionError("must not abort when there is nothing to measure")

    await gpu_guard.guarded_load(
        load, probe=probe, projected_gb=21.0, target="m", abort=abort, sample_interval_s=0.01
    )
    assert finished


async def test_watchdog_surfaces_the_loads_own_failure_unchanged() -> None:
    # The guard must not mask an ordinary load error (a bad GGUF, a gateway 500) behind a
    # memory story — that would send someone hunting the wrong problem.
    probe = _ScriptedProbe([_sample(1.0)])

    async def load() -> None:
        raise RuntimeError("llama-server refused the model")

    async def abort() -> None:
        raise AssertionError("a failed load is not a runaway")

    with pytest.raises(RuntimeError, match="refused the model"):
        await gpu_guard.guarded_load(
            load, probe=probe, projected_gb=1.0, target="m", abort=abort, sample_interval_s=0.01
        )


async def test_abort_failure_still_raises_the_budget_error() -> None:
    # Releasing is best-effort: if the unload also fails (a wedged gateway is exactly the
    # state a runaway produces) the caller must STILL learn the load was aborted, rather
    # than getting the unload's error or, worse, silence.
    probe = _ScriptedProbe([_sample(1.0), _sample(99.0)])

    async def load() -> None:
        await asyncio.sleep(5)

    async def abort() -> None:
        raise RuntimeError("gateway unreachable")

    with pytest.raises(GpuBudgetError):
        await gpu_guard.guarded_load(
            load, probe=probe, projected_gb=2.0, target="m", abort=abort, sample_interval_s=0.01
        )


# --- measurement -----------------------------------------------------------


async def test_measure_footprint_reports_what_a_load_actually_pinned() -> None:
    # The reading that turns a catalog guess into a fact: what the model REALLY cost, which
    # is the number a future budget should use instead of size_gb + kv_gb.
    probe = _ScriptedProbe([_sample(30.0)])
    got = await gpu_guard.measure_footprint(probe, _sample(8.0), "m")
    assert got == pytest.approx(22.0, abs=0.01)


async def test_measure_footprint_is_none_without_both_readings() -> None:
    probe = _ScriptedProbe([None])
    assert await gpu_guard.measure_footprint(probe, _sample(8.0), "m") is None
    assert await gpu_guard.measure_footprint(_ScriptedProbe([_sample(9.0)]), None, "m") is None


# --- the probe -------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: Any, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._body


class _FakeClient:
    def __init__(self, resp: Any = None, boom: bool = False) -> None:
        self._resp = resp
        self._boom = boom
        self.calls: list[str] = []

    async def get(self, url: str, headers: Any = None) -> Any:
        self.calls.append(url)
        if self._boom:
            raise RuntimeError("supervisor unreachable")
        return self._resp


async def test_supervisor_probe_reads_metrics() -> None:
    body = {
        "gpu_mem": {
            "gtt_used_bytes": int(10.0 * _GB),
            "gtt_total_bytes": int(120.0 * _GB),
            "vram_used_bytes": 0,
            "vram_total_bytes": int(2.0 * _GB),
        }
    }
    client = _FakeClient(_FakeResp(body))
    got = await gpu_guard.SupervisorGpuMemProbe(client, token="t").sample()
    assert got is not None and got.gtt_used_gb == pytest.approx(10.0, abs=0.01)
    assert client.calls == ["/metrics"]


async def test_supervisor_probe_degrades_when_unreachable() -> None:
    # An unreachable supervisor must read as "unknown", which leaves loads working exactly
    # as they do today — not as a refusal, and not as a fabricated zero.
    assert await gpu_guard.SupervisorGpuMemProbe(_FakeClient(boom=True)).sample() is None


async def test_watchdog_catches_an_allocation_that_lands_as_the_load_returns() -> None:
    # The hole a purely in-flight watchdog would leave. A load can report success with its
    # device allocation still settling, or simply finish between two samples — which is the
    # normal case for a fast load. Without the post-load check the guard would be blind for
    # exactly one sampling interval, and that is enough to lose the box.
    probe = _ScriptedProbe([_sample(2.0), _sample(95.0)])
    aborted: list[str] = []

    async def load() -> None:
        return  # completes immediately — the watchdog loop never gets to sample

    async def abort() -> None:
        aborted.append("unloaded")

    with pytest.raises(GpuBudgetError) as exc:
        await gpu_guard.guarded_load(
            load, probe=probe, projected_gb=21.0, target="m", abort=abort, sample_interval_s=0.01
        )
    assert "finished loading" in str(exc.value)
    assert aborted == ["unloaded"], "a model that overshot after loading must be released"
