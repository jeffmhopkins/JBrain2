"""The post-upgrade gateway smoke test (jbrain.llm.smoketest).

Drives the real catalog through an in-memory gateway so the load/probe SELECTION
and the pass/fail contract are exercised without a live llama.cpp: the update
path keeps a floated-to-newest build only when this returns ok=True, else rolls
back to the pinned base.
"""

import pytest

from jbrain.llm import smoketest
from jbrain.llm.local_gateway import LocalGatewayError


def _meminfo(tmp_path, available_gb: float | None, name: str = "meminfo"):
    """A /proc/meminfo stand-in. `None` omits the field, which is how a host that
    reports no MemAvailable must be exercised."""
    path = tmp_path / name
    body = "MemTotal:       131072000 kB\n"
    if available_gb is not None:
        body += f"MemAvailable:   {int(available_gb * 1024 * 1024)} kB\n"
    path.write_text(body)
    return path


@pytest.fixture(autouse=True)
def _ample_memory(tmp_path, monkeypatch):
    """Pin the headroom gate to a box with room, for every test that isn't about the
    gate. Without it the suite reads the RUNNER's free RAM and its result depends on
    what else CI happens to be doing — a smoke test asserting `gw.loaded == [...]`
    would go red on a busy machine for reasons having nothing to do with the code."""
    monkeypatch.setattr(smoketest, "MEMINFO_PATH", _meminfo(tmp_path, 512, "ample"))


class _FakeGateway:
    """Records load/probe calls; can be told to fail either, mirroring how a bad
    llama.cpp build surfaces (LocalGatewayError on the health/probe request)."""

    def __init__(
        self,
        *,
        fail_load: bool = False,
        fail_probe: bool = False,
        resident: set[str] | None = None,
    ) -> None:
        self.fail_load = fail_load
        self.fail_probe = fail_probe
        self.loaded: list[str] = []
        self.probed: list[str] = []
        # What the gateway already holds. A resident model costs nothing to "load", so
        # the headroom gate must not charge for it.
        self.resident = resident if resident is not None else set()

    async def running(self) -> set[str]:
        return self.resident | set(self.loaded)

    async def load(self, served_model: str) -> None:
        if self.fail_load:
            raise LocalGatewayError("simulated load crash")
        self.loaded.append(served_model)

    async def tool_probe(self, served_model: str) -> None:
        if self.fail_probe:
            raise LocalGatewayError("simulated tool-grammar crash")
        self.probed.append(served_model)


async def test_loads_smallest_installed_tool_capable_model_then_probes_gpt_oss() -> None:
    gw = _FakeGateway()
    ok, messages = await smoketest.run_smoketest(["gpt-oss-120b", "qwen3.5-0.8b", "qwen3.5-4b"], gw)
    assert ok
    # Smallest installed tool-capable model is qwen3.5-0.8b (~0.9 GiB) — the cheapest
    # possible load, so a broken build fails without reading a big model's weights.
    assert gw.loaded == ["qwen3.5-0.8b"]
    # gpt-oss installed → the tool-call regression guard runs against its served name.
    assert gw.probed == ["gpt-oss-120b"]


async def test_load_failure_fails_the_smoke_and_skips_the_probe() -> None:
    gw = _FakeGateway(fail_load=True)
    ok, messages = await smoketest.run_smoketest(["gpt-oss-120b", "qwen3.5-0.8b"], gw)
    assert not ok
    assert gw.probed == []  # never reached the probe
    assert any("load FAILED" in m for m in messages)


async def test_tool_probe_failure_fails_the_smoke() -> None:
    # The load succeeds but the tool-carrying turn crashes (the past gpt-oss regression):
    # still a rollback signal.
    gw = _FakeGateway(fail_probe=True)
    ok, messages = await smoketest.run_smoketest(["gpt-oss-120b", "qwen3.5-0.8b"], gw)
    assert not ok
    assert gw.loaded == ["qwen3.5-0.8b"]
    assert any("tool-call probe FAILED" in m for m in messages)


async def test_no_gpt_oss_installed_skips_the_tool_probe() -> None:
    gw = _FakeGateway()
    ok, _ = await smoketest.run_smoketest(["qwen3.5-4b", "qwen3.5-0.8b"], gw)
    assert ok
    assert gw.loaded == ["qwen3.5-0.8b"]
    assert gw.probed == []  # no gpt-oss → no tool probe


async def test_no_installed_tool_capable_models_is_a_pass_noop() -> None:
    gw = _FakeGateway()
    ok, messages = await smoketest.run_smoketest([], gw)
    assert ok
    assert gw.loaded == [] and gw.probed == []
    assert any("no installed" in m for m in messages)


async def test_unknown_ids_are_ignored() -> None:
    # selected() drops ids outside the catalog, so a stale/unknown entry never picks a load.
    gw = _FakeGateway()
    ok, _ = await smoketest.run_smoketest(["not-a-real-model", "qwen3.5-0.8b"], gw)
    assert ok
    assert gw.loaded == ["qwen3.5-0.8b"]


# --- the headroom gate -------------------------------------------------------
#
# Loading a model pins its weights in unified memory through the amdgpu GTT, and the
# driver asks with __GFP_RETRY_MAYFAIL: the kernel reclaims hard and then FAILS the
# allocation rather than OOM-killing. So an over-tight load kills nothing, logs nothing,
# and livelocks the host in reclaim — a freeze down to the USB keyboard, needing a power
# cycle. It happened repeatedly during updates, and the trace caught llama-server failing
# an order:0 (single 4 KB page) allocation inside amdgpu_ttm_tt_populate.
#
# So the smoke test refuses a load it cannot afford. Refusing reads as a smoke FAILURE,
# which routes to the update's existing rollback: a box without the room keeps the
# pinned, known-good base rather than betting the host on a verification step.


async def test_refuses_the_load_when_the_box_has_no_headroom(tmp_path) -> None:
    gw = _FakeGateway()
    # 4 GB free against qwen3.5-0.8b's 1.4 GB resident cost: enough for the model itself,
    # nowhere near LOAD_HEADROOM_GB beyond it — which is the margin that matters.
    ok, messages = await smoketest.run_smoketest(
        ["qwen3.5-0.8b"], gw, meminfo_path=_meminfo(tmp_path, 4)
    )
    assert not ok, "no headroom must read as a smoke failure, so the update rolls back"
    assert gw.loaded == [], "the whole point is that the load never happens"
    assert any("NOT ENOUGH MEMORY" in m for m in messages)


async def test_refuses_the_expensive_tool_probe_while_allowing_the_cheap_load(
    tmp_path,
) -> None:
    # The load picks the SMALLEST model (1.4 GB resident) but the probe loads gpt-oss-120b
    # (63.5 GB) — it is the probe, not the cheap load, that the kernel trace caught
    # freezing the box. 40 GB clears the first and must not clear the second.
    gw = _FakeGateway()
    ok, messages = await smoketest.run_smoketest(
        ["gpt-oss-120b", "qwen3.5-0.8b"], gw, meminfo_path=_meminfo(tmp_path, 40)
    )
    assert not ok
    assert gw.loaded == ["qwen3.5-0.8b"], "the cheap load fits and must still run"
    assert gw.probed == [], "the 63.5 GB probe must be refused"
    assert any("NOT ENOUGH MEMORY" in m and "gpt-oss-120b" in m for m in messages)


async def test_a_box_with_room_runs_the_whole_smoke_test(tmp_path) -> None:
    gw = _FakeGateway()
    ok, messages = await smoketest.run_smoketest(
        ["gpt-oss-120b", "qwen3.5-0.8b"], gw, meminfo_path=_meminfo(tmp_path, 110)
    )
    assert ok
    assert gw.loaded == ["qwen3.5-0.8b"] and gw.probed == ["gpt-oss-120b"]
    assert any("110 GB available" in m for m in messages)


async def test_unreadable_meminfo_proceeds_rather_than_blocking(tmp_path) -> None:
    # On any real deployment /proc/meminfo is readable, so "unknown" means an
    # environment odd enough that refusing every load would be the wrong default.
    # It is said out loud in the log rather than passing silently.
    gw = _FakeGateway()
    ok, messages = await smoketest.run_smoketest(
        ["qwen3.5-0.8b"], gw, meminfo_path=tmp_path / "does-not-exist"
    )
    assert ok
    assert gw.loaded == ["qwen3.5-0.8b"]
    assert any("MemAvailable unreadable" in m for m in messages)


def test_mem_available_reads_the_kb_field_as_gb(tmp_path) -> None:
    assert smoketest.mem_available_gb(_meminfo(tmp_path, 64)) == 64.0
    # Field absent (a kernel/container that reports no MemAvailable) is unknown, not zero
    # — zero would refuse every load forever.
    assert smoketest.mem_available_gb(_meminfo(tmp_path, None)) is None


async def test_an_already_resident_model_is_never_gated(tmp_path) -> None:
    """A load that allocates nothing must not be refused for lack of room to allocate.

    This is not a corner case. On a box whose only tool-capable model IS gpt-oss, step 1
    loads it and step 2 wants the very same model — so charging for it again refuses
    every update forever, deterministically, while the box sits there with the model
    already loaded. The keep-warm prime in the running api creates the same situation.
    """
    gw = _FakeGateway()
    # 70 GB clears gpt-oss's first load (63.5 + 20 = 83.5 would NOT) only because the
    # probe sees it as resident; make the point sharply by giving it just enough for the
    # first load and nowhere near enough for a second.
    ok, messages = await smoketest.run_smoketest(
        ["gpt-oss-120b"], gw, meminfo_path=_meminfo(tmp_path, 90)
    )
    assert ok, f"gpt-oss-only box must be able to pass: {messages}"
    assert gw.loaded == ["gpt-oss-120b"] and gw.probed == ["gpt-oss-120b"]
    assert any("already resident" in m for m in messages)


async def test_an_unreadable_roster_gates_rather_than_crashing(tmp_path) -> None:
    """`running()` failing must not be read as "everything is resident"."""

    class _Blind(_FakeGateway):
        async def running(self) -> set[str]:
            raise LocalGatewayError("roster unreachable")

    gw = _Blind()
    ok, messages = await smoketest.run_smoketest(
        ["qwen3.5-0.8b"], gw, meminfo_path=_meminfo(tmp_path, 4)
    )
    assert not ok and gw.loaded == []
    assert any("NOT ENOUGH MEMORY" in m for m in messages)


def test_the_headroom_matches_the_apps_own_residency_floor() -> None:
    """The update path is the one that has actually frozen this hardware. It must not be
    the loosest load path on the box.

    Every ordinary turn loads through `jbrain.llm.residency`, which keeps
    `local_llm_free_ram_fraction` of RAM free. This test is what stops the two numbers
    drifting apart — the same guard the deploy suite puts on the sysctl values.
    """
    from jbrain.config import Settings

    fraction = Settings.model_fields["local_llm_free_ram_fraction"].default
    box_ram_gb = 130.0  # the reference Strix Halo box the floor was tuned for
    turn_floor_gb = fraction * box_ram_gb
    assert turn_floor_gb <= smoketest.LOAD_HEADROOM_GB, (
        f"the smoke test would load with less headroom ({smoketest.LOAD_HEADROOM_GB} GB) "
        f"than a normal turn requires ({turn_floor_gb:.1f} GB)"
    )
