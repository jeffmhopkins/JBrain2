"""The post-upgrade gateway smoke test (jbrain.llm.smoketest).

Drives the real catalog through an in-memory gateway so the load/probe SELECTION
and the pass/fail contract are exercised without a live llama.cpp: the update
path keeps a floated-to-newest build only when this returns ok=True, else rolls
back to the pinned base.
"""

from jbrain.llm import smoketest
from jbrain.llm.local_gateway import LocalGatewayError


class _FakeGateway:
    """Records load/probe calls; can be told to fail either, mirroring how a bad
    llama.cpp build surfaces (LocalGatewayError on the health/probe request)."""

    def __init__(self, *, fail_load: bool = False, fail_probe: bool = False) -> None:
        self.fail_load = fail_load
        self.fail_probe = fail_probe
        self.loaded: list[str] = []
        self.probed: list[str] = []

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
