"""The device-memory guard must be unbypassable.

Three host freezes — each needing a power cycle — came through a `gateway.load()` that never
reached the residency coordinator's guard: the settings screen's deliberate load, the debug
console's, and the coordinator's own end-of-turn restore. The guard now lives inside
`LocalGatewayClient.load`, the single chokepoint. These tests hold that property in place:
the first is a STRUCTURAL check that fails CI the moment a new unguarded gateway is built,
because a reviewer noticing is what already failed three times.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import httpx
import pytest

from jbrain.llm import gpu_guard, local_catalog
from jbrain.llm.local_gateway import LocalGatewayClient

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"


def _gateway_constructions() -> list[tuple[pathlib.Path, ast.Call]]:
    found: list[tuple[pathlib.Path, ast.Call]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "LocalGatewayClient"
            ):
                found.append((path, node))
    return found


def test_every_local_gateway_in_src_is_built_with_a_gpu_probe() -> None:
    """No production code path may construct a local-model gateway without the probe.

    Whisper gateways are exempt: they only ever unload. Anything pointed at
    `local_llm_url` can load, and a load with no probe is the exact hole this closes."""
    constructions = _gateway_constructions()
    assert constructions, "the AST scan found no gateways at all — the check has rotted"
    unguarded = []
    for path, call in constructions:
        first = call.args[0] if call.args else None
        target = ast.unparse(first) if first is not None else ""
        if "whisper" in target:
            continue
        if not any(kw.arg == "gpu_probe" for kw in call.keywords):
            unguarded.append(f"{path.relative_to(_SRC)}:{call.lineno}")
    assert not unguarded, (
        "LocalGatewayClient built without gpu_probe= at: "
        + ", ".join(unguarded)
        + " — every load path must pass the device-memory guard (jbrain.llm.gpu_guard)."
    )


class _StubProbe:
    def __init__(self, *samples: gpu_guard.GpuMem) -> None:
        self._samples = list(samples)
        self.calls = 0

    async def sample(self) -> gpu_guard.GpuMem | None:
        self.calls += 1
        return self._samples[min(self.calls - 1, len(self._samples) - 1)]


def _mem(used: float, total: float = 124.0) -> gpu_guard.GpuMem:
    return gpu_guard.GpuMem(
        gtt_used_gb=used, gtt_total_gb=total, vram_used_gb=0.0, vram_total_gb=0.0
    )


@pytest.mark.anyio
async def test_load_refuses_when_the_device_pool_is_short() -> None:
    """The freeze, reproduced as a refusal: a box already holding gpt-oss, asked for the
    projector-carrying q4. Its `load_footprint_gb` includes the projector balloon, so the
    pre-flight sees ~50 GB against ~30 GB of headroom and declines."""
    probe = _StubProbe(_mem(87.2))
    gateway = LocalGatewayClient("http://gw", gpu_probe=probe)
    with pytest.raises(gpu_guard.GpuBudgetError) as exc:
        await gateway.load("qwen3.8-27b-q4")
    assert "refusing to load" in str(exc.value)


def _transport(*, delay_s: float = 0.0, seen: list[str] | None = None) -> httpx.MockTransport:
    """A gateway that answers /health (the load), the warm-up, and the unload."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path.endswith("/health") and delay_s:
            await asyncio.sleep(delay_s)
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_load_without_a_probe_keeps_the_prior_behaviour() -> None:
    """A box that can't measure its device pool (no amdgpu, supervisor down) must still be
    able to serve. Absence of a probe means 'we don't know', never a refusal."""
    gateway = LocalGatewayClient("http://gw", transport=_transport())
    await gateway.load("qwen3.8-27b-q4")  # would be refused outright if a probe were wired


@pytest.mark.anyio
async def test_a_probe_that_cannot_read_the_pool_does_not_block_the_load() -> None:
    """Same rule one level down: a wired probe returning None is still 'we don't know'."""

    class _Blind:
        async def sample(self) -> gpu_guard.GpuMem | None:
            return None

    gateway = LocalGatewayClient("http://gw", transport=_transport(), gpu_probe=_Blind())
    await gateway.load("qwen3.8-27b-q4")


@pytest.mark.anyio
async def test_a_load_that_balloons_mid_flight_is_aborted_and_unloaded() -> None:
    """The leg that matters most, exercised THROUGH `load()`: the pre-flight passed, and the
    model then allocated far past its prediction anyway. An estimate can only be wrong in
    ways already seen, so the watchdog is the only check that protects the FIRST load of
    anything new — and here it must abort without the caller having asked for a watchdog."""
    # Roomy for the pre-flight and the watchdog's baseline, then GTT runs away mid-load.
    probe = _StubProbe(_mem(10.0), _mem(10.0), _mem(118.0))
    seen: list[str] = []
    gateway = LocalGatewayClient(
        "http://gw", transport=_transport(delay_s=5.0, seen=seen), gpu_probe=probe, timeout=30.0
    )
    with pytest.raises(gpu_guard.GpuBudgetError) as exc:
        await gateway.load("qwen3.8-27b-mtp")
    assert "aborted" in str(exc.value)
    assert any("/unload/qwen3.8-27b-mtp" in path for path in seen), seen


def test_load_footprint_carries_the_projector_balloon() -> None:
    """`footprint_gb` answers 'what does this hold while resident'; `load_footprint_gb`
    answers 'what must be free for the load to be safe'. The gap is the mmproj balloon
    (llama.cpp #27146), and omitting it is what every freeze had in common."""
    with_projector = local_catalog.get("qwen3.8-27b-q4")
    without = local_catalog.get("qwen3.8-27b-mtp")
    assert with_projector is not None and without is not None
    assert with_projector.mmproj_include and not without.mmproj_include
    assert local_catalog.load_footprint_gb(with_projector) > (
        with_projector.size_gb + gpu_guard.MIN_FREE_GTT_GB
    )
    assert (
        local_catalog.load_footprint_gb(with_projector) - local_catalog.load_footprint_gb(without)
        >= local_catalog.PROJECTOR_GTT_OVERHEAD_GB - 10
    )
    assert local_catalog.load_footprint_gb(without) < 25.0
