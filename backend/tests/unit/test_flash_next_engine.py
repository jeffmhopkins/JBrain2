"""Flash-Next F1 (docs/plans/FLASH_NEXT_ENGINE_PLAN.md): the catalog entry and its budget,
default slots, and engine separation on every surface that offers, loads or reads a model.

The config rendering itself is covered in test_llama_swap_config.py and the standard-file
regression in test_llama_swap_golden.py."""

import ast
import contextlib
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jbrain.api import llm_settings
from jbrain.llm import (
    admission,
    engine,
    gpu_guard,
    llama_swap_config,
    local_catalog,
    local_gateway,
    local_weights,
    providers,
    slot_roles,
    smoketest,
)
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError, ResidencyWiring
from tests.unit.fakes import FakeLocalGateway
from tests.unit.test_llm_settings_api import _authed_client, _cloud_settings

FLASH_ID = "qwen3.8-flash-next"
# A catalog model the prompt cache saves for, so its fingerprint is not gated off.
SAVER = "qwen3-vl-30b-a3b"


def _flash() -> local_catalog.LocalModel:
    model = local_catalog.get(FLASH_ID)
    assert model is not None
    return model


# --- the catalog entry ------------------------------------------------------------------


def test_the_entry_is_a_flash_next_engine_model_with_an_eight_slot_pool() -> None:
    m = _flash()
    assert m.engine == engine.FLASH_NEXT
    pool = m.kv_pool
    assert pool is not None and pool is slot_roles.FLASH_NEXT_POOL
    assert m.default_slots == pool.n_slots == 8
    assert pool.n_ctx == 524_288
    assert m.served_model == FLASH_ID and m.spec == f"local:{FLASH_ID}"
    assert m.hf_repo == "unsloth/Qwen3.8-Flash-Next-GGUF"
    assert m.quant == "UD-IQ4_XS" and "UD-IQ4_XS" in m.gguf_include
    assert m.mmproj_include == "mmproj-F16.gguf"
    assert m.context_window == m.native_context_window == 262144
    assert m.recurrent and m.supports_vision and m.supports_tools and m.supports_reasoning
    assert m.hybrid_thinking and m.thinking_effort_map == local_catalog.QWEN38_EFFORT_LEVELS
    assert not m.recommended and not m.is_speculative
    # Same Qwen3.8 sampling split as the 27B family.
    qwen = local_catalog.get("qwen3.8-27b")
    assert qwen is not None
    assert m.sampling == qwen.sampling and m.sampling_thinking == qwen.sampling_thinking
    # GiB, the catalog's unit: 93.7 decimal GB of shards + the 0.84 GiB projector.
    assert m.size_gb == pytest.approx(93.7e9 / 1024**3 + 0.84, abs=0.1)
    # The 26.82 GiB engram table plus the ~1.6 GiB more the F2 fit shows never reaches the GPU.
    assert m.file_backed_gb == pytest.approx(28.4)


def test_every_standard_entry_keeps_one_default_slot_and_no_file_backed_weights() -> None:
    for m in local_catalog.CATALOG:
        if m.engine == engine.STANDARD:
            assert m.default_slots == 1 and m.file_backed_gb == 0.0, m.id
            assert m.served_ctx_checkpoints == 0, m.id


def test_kv_term_is_the_f2_measured_slope() -> None:
    """§3a: GTT grew 7.0 GiB per 262,144 cells across five cold-load layouts — 3.5 per 128k,
    ~1.75x the §3 header derivation (attention KV + QSA indexer, both q8_0)."""
    derived = (12 * 2 * 256 * 2 * 1.0625 + 12 * 256 * 1.0625) * 131072 / 1024**3
    assert _flash().kv_gb_per_128k == pytest.approx(7.0 / 2)
    assert _flash().kv_gb_per_128k / derived == pytest.approx(1.75, abs=0.03)


def test_checkpoints_are_pinned_at_eight_per_slot() -> None:
    m = _flash()
    assert local_catalog.ctx_checkpoints(m.checkpoint_gb, m.served_ctx_checkpoints) == 8
    # Without the pin, the measured-cost rule would hand a derived figure 16.
    assert local_catalog.ctx_checkpoints(m.checkpoint_gb) == local_catalog.CTX_CHECKPOINTS


def test_device_footprint_lands_on_the_f2_fit_for_the_pool() -> None:
    """§3a fit: GTT ≈ 60.2 + 7.0 per 262,144 cells = 74.2 GiB for the 512k pool. The fixed 60.2
    already holds the projector and vision workspace, so the booking must not add them twice."""
    m = _flash()
    fit = 60.2 + 7.0 * 524_288 / 262_144
    _host, device = local_catalog.declared_gb(m, 262144)
    assert device == pytest.approx(fit, abs=0.1)
    fixed = (m.size_gb - m.file_backed_gb) + local_catalog.vision_attn_buffer_gb()
    assert fixed == pytest.approx(60.2, abs=0.1)
    # Eviction and the meter add the 64 host-only checkpoints (8 per pool slot) on top.
    footprint = local_catalog.footprint_gb(m, 262144)
    assert footprint == pytest.approx(device + 0.11 * 8 * 8, abs=0.02)


def test_file_backed_weights_are_subtracted_from_the_measured_disk_size() -> None:
    m = _flash()
    at_disk = local_catalog.footprint_gb(m, 262144, disk_gb=90.0)
    # Every GB on disk above the file-backed share is a resident GB; the share itself is not.
    assert at_disk - local_catalog.footprint_gb(m, 262144, disk_gb=89.0) == pytest.approx(1.0)
    no_backing = dataclasses.replace(m, file_backed_gb=0.0)
    assert at_disk == pytest.approx(
        local_catalog.footprint_gb(no_backing, 262144, disk_gb=90.0 - m.file_backed_gb),
        abs=0.01,
    )
    # A partial download can never drive the weights term negative.
    assert local_catalog.footprint_gb(m, 262144, disk_gb=1.0) == local_catalog.footprint_gb(
        m, 262144, disk_gb=0.0
    )
    assert local_catalog.load_footprint_gb(m) < m.size_gb + m.kv_gb_per_128k * 8


def test_admission_leaves_the_lazy_checkpoints_out_for_the_pool_only() -> None:
    """Checkpoints are host RAM made lazily, slot by slot, so a load's charge is the device
    figure; the footprint (eviction, meter) still counts them. Standard entries are unchanged:
    their host column still carries their checkpoints."""
    m = _flash()
    host, device = local_catalog.declared_gb(m, 262144, disk_gb=90.0)
    assert host == device
    assert local_catalog.footprint_gb(m, 262144, disk_gb=90.0) == pytest.approx(
        host + 0.11 * 8 * 8, abs=0.02
    )
    qwen = local_catalog.get("qwen3.8-27b")
    assert qwen is not None and qwen.checkpoint_gb > 0
    q_host, q_device = local_catalog.declared_gb(qwen, qwen.context_window)
    assert q_host - q_device == pytest.approx(
        local_catalog._checkpoints_gb(qwen, 1) + local_catalog.CACHE_RAM_GB, abs=0.02
    )


def test_a_flash_next_load_is_admitted_on_a_lightly_used_128gb_box() -> None:
    """The live box reads ~121 GiB total with ~15 GiB in use before a switch (§3a). Booked at
    the fit with the checkpoints left out, the load is admitted on both pools with room; the
    first cut of the pool (device over-booked, all 64 checkpoints charged) needed ~102.8 GiB
    free and rolled the switch back on any slightly busy box."""
    m = _flash()
    host_gb, device_gb = local_catalog.declared_gb(m, 262144)
    reserve = gpu_guard.MIN_FREE_GTT_GB
    free = 121.0 - 15.0
    request = admission.Reservation(
        "i", m.served_model, admission.Phase.PLANNED, host_gb, device_gb
    )
    pool = admission.Pool(total_gb=121.0, reserve_gb=reserve, measured_free_gb=free)
    decision = admission.admit(request, [], host=pool, device=pool)
    assert decision.outcome is admission.Outcome.ADMIT, decision.reason
    assert host_gb + reserve <= 94.5
    # The headroom left is real, not a rounding margin.
    assert free - reserve - host_gb >= 11.0


def test_the_pool_is_charged_once_whatever_window_or_slots_are_saved() -> None:
    """One `--kv-unified` allocation: a stale window or slot override (F2 stored some) moves
    neither the gateway command nor the budget, so neither may move the footprint."""
    m = _flash()
    pooled = local_catalog.footprint_gb(m, 262144)
    for window, slots in ((65536, 2), (262144, 4), (32768, 1), (262144, None)):
        assert local_catalog.footprint_gb(m, window, slots=slots) == pooled
        assert local_catalog.declared_gb(m, window, slots=slots) == local_catalog.declared_gb(
            m, 262144
        )
        assert local_catalog.load_footprint_gb(m, window, slots=slots) == (
            local_catalog.load_footprint_gb(m)
        )
    kv_only = local_catalog.footprint_gb(m, 262144, disk_gb=0.0) - local_catalog.footprint_gb(
        dataclasses.replace(m, kv_gb_per_128k=0.0), 262144, disk_gb=0.0
    )
    assert m.kv_pool is not None
    assert kv_only == pytest.approx(m.kv_gb_per_128k * m.kv_pool.n_ctx / 131072, abs=0.02)


def test_served_slots_are_the_pool_slots() -> None:
    m = _flash()
    assert m.served_slots({}) == 8 and m.served_slots({FLASH_ID: 2}) == 8
    assert m.effective_slots(1) == m.effective_slots(4) == 8
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None and gpt.served_slots({}) == 1 and gpt.served_slots({gpt.id: 2}) == 2


# --- engine separation: pickers, jcode, residency, smoketest, kv-prefix ------------------


def _settings(**kw: Any) -> Any:
    return SimpleNamespace(
        local_llm_enabled=True,
        local_models=["gpt-oss-120b", "qwen3.5-4b", FLASH_ID],
        local_llm_model="x",
        xai_api_key="",
        anthropic_api_key="",
        **kw,
    )


def test_the_picker_offers_only_the_active_engines_models() -> None:
    standard = {c.id for c in providers.provider_choices(_settings())}
    assert standard == {"gpt-oss-120b", "qwen3.5-4b"}
    flash = {c.id for c in providers.provider_choices(_settings(), engine.FLASH_NEXT)}
    assert flash == {FLASH_ID}


def test_jcode_offers_only_the_active_engines_models() -> None:
    installed = ["gpt-oss-120b", FLASH_ID]
    assert [m.id for m in local_catalog.jcode_models(True, installed)] == ["gpt-oss-120b"]
    assert [m.id for m in local_catalog.jcode_models(True, installed, engine.FLASH_NEXT)] == [
        FLASH_ID
    ]


def _coord(engine_now: engine.Engine, gw: FakeLocalGateway) -> ResidencyCoordinator:
    async def _engine() -> engine.Engine:
        return engine_now

    return ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=True, engine_loader=_engine))


@pytest.mark.asyncio
async def test_residency_refuses_a_model_of_the_engine_that_is_not_running() -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(engine.STANDARD, gw)
    with pytest.raises(ResidencyError, match="Runs on the Flash-Next engine"):
        await coord.ensure_room(FLASH_ID)
    with pytest.raises(ResidencyError, match="Runs on the Flash-Next engine"):
        await coord.free_room(FLASH_ID)
    # Refused BEFORE any eviction: the resident model was not touched for a load that fails.
    assert gw.unloaded == []
    # The operator's deliberate load of a standard model is refused while Flash-Next serves
    # (a completion for one is REMAPPED instead — test_engine_switch.py).
    with pytest.raises(ResidencyError, match="Runs on the Standard engine"):
        await _coord(engine.FLASH_NEXT, gw).free_room("gpt-oss-120b")


@pytest.mark.asyncio
async def test_restore_skips_members_of_the_inactive_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 0.0)
    )
    coord = _coord(engine.FLASH_NEXT, FakeLocalGateway())
    plan = await coord._restore_plan({"gpt-oss-120b", FLASH_ID}, 0.15, {}, {})
    assert plan == [FLASH_ID]


@pytest.mark.asyncio
async def test_residency_budgets_flash_next_at_its_default_slots() -> None:
    coord = _coord(engine.FLASH_NEXT, FakeLocalGateway())
    fp = await coord._footprint(FLASH_ID, {}, {})
    assert fp == local_catalog.footprint_gb(_flash(), 262144, slots=8)


@pytest.mark.asyncio
async def test_gateway_served_shape_defaults_to_the_catalog_slots() -> None:
    client = local_gateway.LocalGatewayClient("http://x/v1")
    assert await client._served_shape(_flash()) == (262144, 8)

    async def _none() -> dict[str, int]:
        return {}

    wired = local_gateway.LocalGatewayClient("http://x/v1", slots_loader=_none)
    assert await wired._served_shape(_flash()) == (262144, 8)


class _SmokeGateway:
    def __init__(self) -> None:
        self.loaded: list[str] = []

    async def running(self) -> set[str]:
        return set(self.loaded)

    async def load(self, served_model: str) -> None:
        self.loaded.append(served_model)

    async def tool_probe(self, served_model: str) -> None:
        return None


@pytest.mark.asyncio
async def test_smoketest_tries_only_the_engine_under_test(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemFree: 536870912 kB\n")
    installed = ["qwen3.5-0.8b", FLASH_ID]
    gw = _SmokeGateway()
    ok, _ = await smoketest.run_smoketest(installed, gw, meminfo_path=meminfo)
    assert ok and gw.loaded == ["qwen3.5-0.8b"]
    gw = _SmokeGateway()
    ok, _ = await smoketest.run_smoketest(
        installed, gw, meminfo_path=meminfo, engine=engine.FLASH_NEXT
    )
    assert ok and gw.loaded == [FLASH_ID]


def test_kv_prefix_fingerprints_the_active_engines_launch_line(tmp_path: Path) -> None:
    save = " --slot-save-path /models/.kvslots/qwen3-vl-30b-a3b"
    (tmp_path / "llama-swap.yaml").write_text(
        f"models:\n  {SAVER}:\n    cmd: llama-server -c 1{save}\n"
    )
    flash_cfg = tmp_path / "llama-swap.flash-next.yaml"
    flash_cfg.write_text(f"models:\n  {SAVER}:\n    cmd: llama-server -c 2{save}\n")
    store = KvPrefixStore(object(), str(tmp_path))  # type: ignore[arg-type]
    standard_fp = store.identity_of(SAVER, "sys", [], None)
    store.set_engine(engine.FLASH_NEXT)
    flash_fp = store.identity_of(SAVER, "sys", [], None)
    assert standard_fp is not None and flash_fp is not None and standard_fp != flash_fp
    built = KvPrefixStore(object(), str(tmp_path), engine=engine.FLASH_NEXT)  # type: ignore[arg-type]
    assert built.identity_of(SAVER, "sys", [], None) == flash_fp
    flash_cfg.unlink()
    assert store.identity_of(SAVER, "sys", [], None) is None


@pytest.mark.asyncio
async def test_the_pools_slot_save_path_does_not_make_kv_prefix_save_or_restore(
    tmp_path: Path,
) -> None:
    """The pool renders `--slot-save-path` for slot erase only; the disk layer gates on the
    catalog (recurrent, no MTP), so it stays out of Flash-Next until F4 — patch on or off."""
    flash = dataclasses.asdict(_flash())
    (tmp_path / FLASH_ID / "UD-IQ4_XS").mkdir(parents=True)
    (tmp_path / FLASH_ID / "UD-IQ4_XS" / "m-UD-IQ4_XS-00001-of-00001.gguf").write_bytes(b"\0")
    (tmp_path / FLASH_ID / "mmproj-F16.gguf").write_bytes(b"\0")
    llama_swap_config.write(str(tmp_path), [flash], engine=engine.FLASH_NEXT)
    line = llama_swap_config.launch_line(str(tmp_path), FLASH_ID, engine.FLASH_NEXT)
    assert line is not None and "--slot-save-path" in line
    for patch in (False, True):
        store = KvPrefixStore(
            FakeLocalGateway(),  # type: ignore[arg-type]
            str(tmp_path),
            patch_active=patch,
            engine=engine.FLASH_NEXT,
        )
        assert store._eligible(FLASH_ID) is None
        assert store._ineligible_reason(FLASH_ID).startswith("recurrent")
        assert not await store.save_after_prime(FLASH_ID, "sys", [], 50_000)
        assert not await store.restore_if_lost(FLASH_ID, "sys", [])
        # Nothing hashes a prefix for it either, and the snapshot names the refusal.
        assert store.identity_of(FLASH_ID, "sys", [], None) is None
        snap = await store.snapshot([(FLASH_ID, "sys", [], None)])
        row = snap["models"][0]  # type: ignore[index]
        assert row["state"] == "ineligible" and row["eligible"] is False


# --- the page-cache drop -----------------------------------------------------------------


def _recording_drop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every page-cache drop: the whole-file one as the model id, the range-aware one
    (mapped tensors kept) as `<id>:keep-mapped`."""
    dropped: list[str] = []

    def _drop(_dir: str, model_id: str) -> float:
        dropped.append(model_id)
        return 1.5

    def _keep_mapped(_dir: str, model_id: str) -> float:
        dropped.append(f"{model_id}:keep-mapped")
        return 0.5

    monkeypatch.setattr(local_weights, "drop_weights_page_cache", _drop)
    monkeypatch.setattr(local_weights, "drop_weights_page_cache_except_mapped", _keep_mapped)
    return dropped


def test_the_drop_lever_keeps_a_resident_mapped_model_engram_and_drops_it_once_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped = _recording_drop(monkeypatch)
    client = local_gateway.LocalGatewayClient("http://x/v1", models_dir=str(tmp_path))
    client._seen_resident = {FLASH_ID, "gpt-oss-120b"}
    freed = client.drop_page_cache([FLASH_ID, "gpt-oss-120b"])
    # Serving from the engram's pages: only the rest of its shards' cache is dropped.
    assert dropped == [f"{FLASH_ID}:keep-mapped", "gpt-oss-120b"]
    assert freed == {FLASH_ID: 0.5, "gpt-oss-120b": 1.5}
    # Unloaded (after a switch back, say): its ~88 GiB of cache is residue, and is dropped.
    client._seen_resident = set()
    freed = client.drop_page_cache([FLASH_ID])
    assert dropped[-1] == FLASH_ID and freed == {FLASH_ID: 1.5}
    assert local_weights.serves_file_backed(FLASH_ID)
    assert not local_weights.serves_file_backed("gpt-oss-120b")


@pytest.mark.asyncio
async def test_unloading_a_mapped_model_drops_its_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    dropped = _recording_drop(monkeypatch)
    client = local_gateway.LocalGatewayClient(
        "http://x/v1",
        models_dir=str(tmp_path),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
    )
    client._seen_resident = {FLASH_ID}
    await client.unload(FLASH_ID)
    assert dropped == [FLASH_ID] and FLASH_ID not in client._seen_resident
    # A `--no-mmap` model's cache was dropped at load; its unload adds nothing.
    await client.unload("gpt-oss-120b")
    assert dropped == [FLASH_ID]


def test_the_gateway_keeps_only_the_engram_cache_after_a_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped = _recording_drop(monkeypatch)
    client = local_gateway.LocalGatewayClient("http://x/v1", models_dir=str(tmp_path))
    client._drop_weights_cache(_flash())
    assert dropped == [f"{FLASH_ID}:keep-mapped"]
    gpt = local_catalog.get("gpt-oss-120b")
    client._drop_weights_cache(gpt)
    assert dropped[-1] == "gpt-oss-120b"


async def test_the_in_load_sweep_drops_a_mapped_models_residue_by_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep used to skip Flash-Next outright, so the ~60 GiB its load reads for the GPU
    piled up in page cache — what put a 1M-pool load under the guard's host floor."""
    import asyncio

    dropped = _recording_drop(monkeypatch)
    cache = iter([1.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0])
    monkeypatch.setattr(local_gateway.host_metrics, "read_page_cache_gb", lambda: next(cache, 3.0))
    monkeypatch.setattr(local_gateway, "_SWEEP_POLL_S", 0.001)
    client = local_gateway.LocalGatewayClient("http://x/v1", models_dir=str(tmp_path))
    task = asyncio.create_task(client._sweep_page_cache_during_load(_flash()))
    for _ in range(50):
        await asyncio.sleep(0.002)
        if dropped:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert dropped and set(dropped) == {f"{FLASH_ID}:keep-mapped"}


async def test_the_guard_relief_drops_by_range_for_a_mapped_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped = _recording_drop(monkeypatch)
    client = local_gateway.LocalGatewayClient("http://x/v1", models_dir=str(tmp_path))
    await client._relieve_host_memory(_flash())
    await client._relieve_host_memory(local_catalog.get("gpt-oss-120b"))
    await client._relieve_host_memory(None)
    assert dropped == [f"{FLASH_ID}:keep-mapped", "gpt-oss-120b"]


# --- the settings API --------------------------------------------------------------------


def _api(engine_now: str | None = None) -> Any:
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b", FLASH_ID])
    c, store = _authed_client(settings)
    if engine_now is not None:
        store.values["llm_local_engine"] = engine_now
        store.values["llm_local_engine_effective"] = engine_now
    return c, store


def test_the_settings_picker_hides_flash_next_while_standard_is_active() -> None:
    c, _ = _api()
    body = c.get("/api/settings/llm").json()
    assert FLASH_ID not in {p["id"] for p in body["providers"]}
    assert "gpt-oss-120b" in {p["id"] for p in body["providers"]}
    # Still listed in the drawer, marked with its engine, so its weights are installable and
    # removable from the PWA.
    row = {m["id"]: m for m in body["local_models"]}[FLASH_ID]
    assert row["engine"] == "flash-next"
    assert row["parallel_slots"] == 8 and row["parallel_slots_max"] == 8
    resp = c.put("/api/settings/llm", json={"tasks": {"agent.turn": {"provider": FLASH_ID}}})
    assert resp.status_code == 422


def test_the_settings_picker_offers_only_flash_next_while_it_is_active() -> None:
    c, _ = _api("flash-next")
    ids = {p["id"] for p in c.get("/api/settings/llm").json()["providers"]}
    assert FLASH_ID in ids and "gpt-oss-120b" not in ids


def test_the_slot_cap_is_per_model() -> None:
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None
    assert llm_settings.slots_max(gpt) == llm_settings.PARALLEL_SLOTS_MAX == 2
    assert llm_settings.slots_max(_flash()) == 8
    c, _ = _api()
    assert (
        c.put(
            "/api/settings/llm/local-models/gpt-oss-120b/parallel-slots", json={"slots": 3}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("slots", [1, 2, 4, 9, 0])
def test_a_pooled_model_refuses_any_slot_change(slots: int) -> None:
    """The router pins roles by slot id; another count would send calls to the wrong slot. A
    409 with a reason the owner can read, not a 422 range error."""
    c, store = _api("flash-next")
    resp = c.put(f"/api/settings/llm/local-models/{FLASH_ID}/parallel-slots", json={"slots": slots})
    assert resp.status_code == 409
    assert "shared 524,288-token memory pool across 8 slots" in resp.json()["detail"]
    assert "slot count is fixed" in resp.json()["detail"]
    assert FLASH_ID not in store.values.get("llm_local_parallel_slots", {})


@pytest.mark.parametrize("window", [65536, 131072, 1])
def test_a_pooled_model_refuses_any_window_change(window: int) -> None:
    c, store = _api("flash-next")
    resp = c.put(
        f"/api/settings/llm/local-models/{FLASH_ID}/context-window",
        json={"context_window": window},
    )
    assert resp.status_code == 409
    assert "context window is fixed" in resp.json()["detail"]
    assert FLASH_ID not in store.values.get("llm_local_context_windows", {})


@pytest.mark.parametrize("slots", [None, 8])
def test_a_pooled_slot_no_op_is_accepted_and_clears_a_stale_override(slots: int | None) -> None:
    """F2 stored overrides for Flash-Next; the no-op PUT is how they are cleared from the PWA."""
    c, store = _api("flash-next")
    store.values["llm_local_parallel_slots"] = {FLASH_ID: 4, "gpt-oss-120b": 2}
    resp = c.put(f"/api/settings/llm/local-models/{FLASH_ID}/parallel-slots", json={"slots": slots})
    assert resp.status_code == 200
    assert store.values["llm_local_parallel_slots"] == {"gpt-oss-120b": 2}
    row = {m["id"]: m for m in resp.json()["local_models"]}[FLASH_ID]
    assert row["parallel_slots"] == 8


@pytest.mark.parametrize("window", [None, 262144])
def test_a_pooled_window_no_op_is_accepted_and_clears_a_stale_override(window: int | None) -> None:
    c, store = _api("flash-next")
    store.values["llm_local_context_windows"] = {FLASH_ID: 65536}
    resp = c.put(
        f"/api/settings/llm/local-models/{FLASH_ID}/context-window",
        json={"context_window": window},
    )
    assert resp.status_code == 200
    assert store.values["llm_local_context_windows"] == {}


def test_the_snapshot_describes_the_pool_and_hides_stale_overrides() -> None:
    c, store = _api("flash-next")
    store.values["llm_local_context_windows"] = {FLASH_ID: 65536}
    store.values["llm_local_parallel_slots"] = {FLASH_ID: 2}
    rows = {m["id"]: m for m in c.get("/api/settings/llm").json()["local_models"]}
    row = rows[FLASH_ID]
    pool = slot_roles.FLASH_NEXT_POOL
    assert row["kv_pool"]["n_ctx"] == 524_288
    assert row["kv_pool"]["slots"] == [
        {
            "slot": r.slot,
            "role": r.role.value,
            "label": r.label,
            "cap": r.cap_tokens,
            "overflow": r.overflow.value if r.overflow is not None else None,
        }
        for r in pool.reservations
    ]
    assert row["kv_pool"]["slots"][0] == {
        "slot": 0,
        "role": "interactive",
        "label": "jerv (chat, omnibox)",
        "cap": 262144,
        "overflow": None,
    }
    by_role = {s["role"]: s for s in row["kv_pool"]["slots"]}
    assert by_role["pet"]["overflow"] == "small"
    assert row["parallel_slots"] == 8 and row["context_window_override"] is None
    # The pool's KV once, whatever is saved.
    assert row["kv_gb"] == local_catalog.footprint_gb(_flash(), 262144, disk_gb=0.0)
    # The memory bar's weights segment leaves out what the GPU never holds.
    assert row["resident_weights_gb"] == pytest.approx(
        local_catalog.resident_weights_gb(_flash(), row["disk_gb"]), abs=0.01
    )
    assert row["resident_weights_gb"] < row["size_gb"] - 28
    gpt = rows["gpt-oss-120b"]
    assert gpt["resident_weights_gb"] == pytest.approx(gpt["disk_gb"] or gpt["size_gb"], abs=0.01)
    assert rows["gpt-oss-120b"]["kv_pool"] is None


def test_a_single_slot_model_stores_exactly_what_it_always_has() -> None:
    """Default-1 semantics are byte-identical: 1 (and null) clear the entry, 2 records it."""
    c, store = _api()
    url = "/api/settings/llm/local-models/gpt-oss-120b/parallel-slots"
    assert c.put(url, json={"slots": 2}).status_code == 200
    assert store.values["llm_local_parallel_slots"] == {"gpt-oss-120b": 2}
    assert c.put(url, json={"slots": 1}).status_code == 200
    assert store.values["llm_local_parallel_slots"] == {}
    assert c.put(url, json={"slots": 2}).status_code == 200
    assert c.put(url, json={"slots": None}).status_code == 200
    assert store.values["llm_local_parallel_slots"] == {}


def test_override_tensor_is_an_allowlisted_value_taking_flag() -> None:
    assert llm_settings._validate_extra_args(["-ot", "per_layer_token_embd=CPU"]) == [
        "-ot",
        "per_layer_token_embd=CPU",
    ]
    assert llm_settings._validate_extra_args(["--override-tensor", "x=CPU", "-ub", "512"]) == [
        "--override-tensor",
        "x=CPU",
        "-ub",
        "512",
    ]


@pytest.mark.asyncio
async def test_the_load_time_restamp_writes_the_active_engines_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flash = _flash()
    (tmp_path / flash.id / "q").mkdir(parents=True)
    (tmp_path / flash.id / "q" / "a-UD-IQ4_XS.gguf").write_bytes(b"\0")
    (tmp_path / flash.id / "mmproj-F16.gguf").write_bytes(b"\0")
    (tmp_path / "gpt-oss-120b").mkdir()
    (tmp_path / "gpt-oss-120b" / "m-mxfp4.gguf").write_bytes(b"\0")
    monkeypatch.setattr(llm_settings, "_GATEWAY_RELOAD_SETTLE_S", 0.0)
    settings = SimpleNamespace(
        local_models=["gpt-oss-120b", flash.id], local_models_dir=str(tmp_path)
    )
    from tests.unit.fakes import FakeSettingsStore

    store = FakeSettingsStore()
    store.values["llm_local_engine_effective"] = "flash-next"
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    assert not (tmp_path / "llama-swap.yaml").exists()
    assert "--load-mode none" in (tmp_path / "llama-swap.flash-next.yaml").read_text()
    store.values["llm_local_engine_effective"] = "standard"
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    assert flash.id not in (tmp_path / "llama-swap.yaml").read_text()


def _lay_down_flash(root: Path) -> None:
    (root / FLASH_ID / "q").mkdir(parents=True)
    (root / FLASH_ID / "q" / "a-UD-IQ4_XS.gguf").write_bytes(b"\0")
    (root / FLASH_ID / "mmproj-F16.gguf").write_bytes(b"\0")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("window", "slots"), [(131072, None), (262144, 1), (131072, 1), (65536, 2), (262144, 4)]
)
async def test_a_saved_f2_layout_no_longer_reaches_the_flash_next_served_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: int, slots: int | None
) -> None:
    """The overrides F2 stored, end to end through the load-time re-stamp: the pool is served
    whatever is saved, and the served shape reads back as one slot's maximum sequence."""
    from tests.unit.fakes import FakeSettingsStore

    _lay_down_flash(tmp_path)
    monkeypatch.setattr(llm_settings, "_GATEWAY_RELOAD_SETTLE_S", 0.0)
    settings = SimpleNamespace(local_models=[FLASH_ID], local_models_dir=str(tmp_path))
    store = FakeSettingsStore()
    store.values["llm_local_engine_effective"] = "flash-next"
    await store.set_llm_local_context_window(None, model_id=FLASH_ID, window=window)
    await store.set_llm_local_parallel_slots(
        None, model_id=FLASH_ID, slots=slots, default=_flash().default_slots
    )
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    text = (tmp_path / "llama-swap.flash-next.yaml").read_text()
    assert " -c 524288 " in text and " -np 8 --kv-unified " in text
    served = _flash().served_model
    assert llama_swap_config_shape(tmp_path)[served] == (262144, 8)


@pytest.mark.asyncio
async def test_a_dropped_stored_flag_is_named_on_the_settings_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-stamp still renders the pool, serves the model without the refused flag, and
    `gateway_config_error` (what the settings screen shows) names the model, the reason and the
    no-shell fix."""
    from tests.unit.fakes import FakeSettingsStore

    _lay_down_flash(tmp_path)
    monkeypatch.setattr(llm_settings, "_GATEWAY_RELOAD_SETTLE_S", 0.0)
    settings = SimpleNamespace(local_models=[FLASH_ID], local_models_dir=str(tmp_path))
    store = FakeSettingsStore()
    store.values["llm_local_engine_effective"] = "flash-next"
    store.values["llm_local_extra_args"] = {FLASH_ID: ["--cache-ram", "8192"]}
    await store.set_llm_local_context_window(None, model_id=FLASH_ID, window=131072)
    try:
        await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
        text = (tmp_path / "llama-swap.flash-next.yaml").read_text()
        assert " -c 524288 " in text and "--cache-ram" not in text
        err = llm_settings.gateway_config_error()
        assert err is not None and FLASH_ID in err and "--cache-ram" in err
        assert f"/api/debug/llm/local-models/{FLASH_ID}/extra-args" in err
    finally:
        llm_settings._set_regen_error(None)


def llama_swap_config_shape(root: Path) -> dict[str, tuple[int, int]]:
    return llama_swap_config.served_shape_from_config(str(root), engine.FLASH_NEXT)


def _local_gateway_clients(module: str) -> list[ast.Call]:
    """Every `LocalGatewayClient(...)` in `module` that talks to the LLM gateway — whether
    named bare or as an attribute, and whether the url is positional or `base_url=`."""
    src = (Path(__file__).resolve().parents[2] / "src" / "jbrain" / f"{module}.py").read_text()
    clients: list[ast.Call] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "LocalGatewayClient":
            continue
        url = node.args[0] if node.args else None
        for kw in node.keywords:
            if kw.arg == "base_url":
                url = kw.value
        if url is not None and "local_llm_url" in ast.unparse(url):
            clients.append(node)
    return clients


def _kw(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


@pytest.mark.parametrize("module", ["main", "worker"])
def test_every_long_lived_llm_gateway_restamps_before_it_loads(module: str) -> None:
    """The window override never reached Flash-Next on the box because the WORKER's client had
    no `config_regen`: the window PUT unloads the model, a background job loads it back, and
    the load ran from the stale file (`n_ctx_slot = 262144` after a 131072 override). Every
    process that loads models must re-stamp first, or an override lands only by luck."""
    clients = _local_gateway_clients(module)
    assert clients, f"{module}.py builds no LLM gateway client any more — update this test"
    for call in clients:
        assert _kw(call, "config_regen") is not None, (
            f"{module}.py builds a LocalGatewayClient without config_regen — its loads serve "
            "whatever llama-swap config was last written, not the saved overrides"
        )


def test_the_worker_asks_the_api_to_restamp_rather_than_writing_itself() -> None:
    """The worker handles untrusted content, so it must never write the gateway config: its
    re-stamp is the api's internal route (jbrain.llm.gateway_regen), not a local write."""
    (call,) = _local_gateway_clients("worker")
    regen = _kw(call, "config_regen")
    assert regen is not None
    assert "gateway_regen.request_regen" in ast.unparse(regen)
    assert "regen_gateway_config" not in ast.unparse(regen)


def test_the_worker_cannot_write_the_models_directory() -> None:
    import yaml

    compose = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"
    volumes = [str(v) for v in yaml.safe_load(compose.read_text())["services"]["worker"]["volumes"]]
    models = [v for v in volumes if "local-models" in v]
    assert models == ["./local-models:/data/local-models:ro"], (
        "the worker's models mount must stay READ-ONLY — a writable one lets a compromised "
        "worker write a llama-swap `cmd:` the gateway executes, or swap a GGUF"
    )


@pytest.mark.parametrize(
    "stored",
    [
        ["--rpc", "10.0.0.1:50052"],
        ["--no-mmap"],
        ["-ot", "x=CPU", "--model-url", "http://evil"],
        ["--ctx-checkpoints", "999"],
        ["-ngl", "x;y"],
    ],
)
def test_the_renderer_re_applies_the_launch_flag_allowlist(
    tmp_path: Path, stored: list[str]
) -> None:
    """A stored override that never went through the settings API's allowlist must not reach
    a launch command — the renderer checks what is STORED, not just what the PUT accepted. The
    model is served on its catalog flags and the reason names the no-shell fix."""
    (tmp_path / "gpt-oss-120b").mkdir()
    (tmp_path / "gpt-oss-120b" / "m-mxfp4.gguf").write_bytes(b"\0")
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None
    rejected: dict[str, str] = {}
    text = llama_swap_config.render(
        [dataclasses.asdict(gpt)],
        str(tmp_path),
        extra_args={"gpt-oss-120b": stored},
        rejected=rejected,
    )
    # Exactly the catalog-only line: nothing of the stored override reached it.
    assert text == llama_swap_config.render([dataclasses.asdict(gpt)], str(tmp_path))
    assert set(rejected) == {"gpt-oss-120b"}
    assert "/api/debug/llm/local-models/gpt-oss-120b/extra-args" in rejected["gpt-oss-120b"]
    # An allowlisted override still renders.
    text = llama_swap_config.render(
        [dataclasses.asdict(gpt)],
        str(tmp_path),
        extra_args={"gpt-oss-120b": ["--ctx-checkpoints", "8", "--swa-full"]},
    )
    assert "--ctx-checkpoints 8" in text and "--swa-full" in text


# --- one TTL-cached engine read, seen without a restart -----------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_active_engine_caches_for_its_ttl_and_invalidates_on_a_switch() -> None:
    value = {"engine": "standard", "reads": 0}

    async def _load() -> str:
        value["reads"] += 1
        return value["engine"]

    clock = _Clock()
    cache = engine.ActiveEngine(_load, ttl_s=5.0, clock=clock)
    assert await cache.get() == engine.STANDARD
    value["engine"] = "flash-next"
    clock.now = 4.0
    assert await cache.get() == engine.STANDARD and value["reads"] == 1
    clock.now = 5.0
    assert await cache.get() == engine.FLASH_NEXT and value["reads"] == 2
    value["engine"] = "standard"
    engine.invalidate_cached()  # what set_llm_local_engine_effective does in this process
    assert await cache.get() == engine.STANDARD and value["reads"] == 3


@pytest.mark.asyncio
async def test_active_engine_keeps_the_last_value_when_a_read_fails() -> None:
    calls = {"n": 0}

    async def _load() -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db blink")
        return "flash-next"

    cache = engine.ActiveEngine(_load, ttl_s=0.0)
    assert await cache.get() == engine.FLASH_NEXT
    assert await cache.get() == engine.FLASH_NEXT


@pytest.mark.asyncio
async def test_settings_store_switch_invalidates_the_cache() -> None:
    from jbrain.settings_store import SqlSettingsStore

    saved: dict[str, object] = {}

    async def _upsert(_ctx: object, key: str, value: object) -> None:
        saved[key] = value

    async def _get(_ctx: object, key: str, default: object) -> object:
        return saved.get(key, default)

    store = SqlSettingsStore.__new__(SqlSettingsStore)
    store.upsert = _upsert  # type: ignore[method-assign]
    store.get = _get  # type: ignore[method-assign]
    cache = engine.ActiveEngine(lambda: store.llm_local_engine_effective(None), ttl_s=3600.0)  # type: ignore[arg-type]
    assert await cache.get() == engine.STANDARD
    await store.set_llm_local_engine_effective(None, "flash-next")  # type: ignore[arg-type]
    assert await cache.get() == engine.FLASH_NEXT


@pytest.mark.asyncio
async def test_the_desired_engine_never_moves_the_effective_one() -> None:
    """The owner's choice and what serves are separate keys: a desire for Flash-Next that
    could not be met (the deploy fell back) leaves the api on standard."""
    from jbrain.settings_store import SqlSettingsStore

    saved: dict[str, object] = {}

    async def _upsert(_ctx: object, key: str, value: object) -> None:
        saved[key] = value

    async def _get(_ctx: object, key: str, default: object) -> object:
        return saved.get(key, default)

    store = SqlSettingsStore.__new__(SqlSettingsStore)
    store.upsert = _upsert  # type: ignore[method-assign]
    store.get = _get  # type: ignore[method-assign]
    await store.set_llm_local_engine(None, "flash-next")  # type: ignore[arg-type]
    assert await store.llm_local_engine(None) == engine.FLASH_NEXT  # type: ignore[arg-type]
    assert await store.llm_local_engine_effective(None) == engine.STANDARD  # type: ignore[arg-type]
    await store.set_llm_local_engine_effective(None, "flash-next")  # type: ignore[arg-type]
    await store.set_llm_local_engine_effective(None, "gpt-9")  # type: ignore[arg-type]
    assert await store.llm_local_engine_effective(None) == engine.STANDARD  # type: ignore[arg-type]
    assert await store.llm_local_engine(None) == engine.FLASH_NEXT  # type: ignore[arg-type]


def test_a_fallback_leaves_the_api_on_the_engine_that_is_up() -> None:
    """Flash-Next DESIRED but the deploy fell back to standard (no image, incomplete weights,
    a failed start): every list keys off the EFFECTIVE engine, so the standard models stay
    offered and the Flash-Next entry stays hidden — no load is refused for the engine up."""
    c, store = _api()
    store.values["llm_local_engine"] = "flash-next"
    store.values["llm_local_engine_effective"] = "standard"
    ids = {p["id"] for p in c.get("/api/settings/llm").json()["providers"]}
    assert "gpt-oss-120b" in ids and FLASH_ID not in ids


def test_the_up_predicate_counts_a_crash_looping_engine() -> None:
    for state in ("running", "paused", "restarting"):
        assert engine.holds_memory(state)
    for state in ("exited", "created", "dead", "missing", ""):
        assert not engine.holds_memory(state)


@pytest.mark.asyncio
async def test_kv_prefix_follows_an_engine_switch_without_a_restart(tmp_path: Path) -> None:
    save = " --slot-save-path /models/.kvslots/qwen3-vl-30b-a3b"
    (tmp_path / "llama-swap.yaml").write_text(
        f"models:\n  {SAVER}:\n    cmd: llama-server -c 1{save}\n"
    )
    (tmp_path / "llama-swap.flash-next.yaml").write_text(
        f"models:\n  {SAVER}:\n    cmd: llama-server -c 2{save}\n"
    )
    current = {"engine": "standard"}

    async def _load() -> str:
        return current["engine"]

    source = engine.ActiveEngine(_load, ttl_s=0.0)
    store = KvPrefixStore(object(), str(tmp_path), engine=source)  # type: ignore[arg-type]
    await store._refresh_engine()
    standard_fp = store.identity_of(SAVER, "sys", [], None)
    current["engine"] = "flash-next"  # the debug route flips it on the live api
    await store._refresh_engine()
    flash_fp = store.identity_of(SAVER, "sys", [], None)
    assert standard_fp is not None and flash_fp is not None and standard_fp != flash_fp


@pytest.mark.asyncio
async def test_residency_reads_the_engine_through_the_shared_cache() -> None:
    reads = {"n": 0}

    async def _load() -> str:
        reads["n"] += 1
        return "standard"

    cache = engine.ActiveEngine(_load, ttl_s=60.0)
    coord = ResidencyCoordinator(
        FakeLocalGateway(running={"gpt-oss-120b"}),
        ResidencyWiring.inert(enabled=True, engine_loader=cache.get),
    )
    for _ in range(5):
        with pytest.raises(ResidencyError):
            await coord.ensure_room(FLASH_ID)
    assert reads["n"] == 1


@pytest.mark.asyncio
async def test_jcode_proxy_lists_the_active_engines_models() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from jbrain.api import jcode_llm

    app = FastAPI()
    app.include_router(jcode_llm.router, prefix="/api")
    app.state.settings = SimpleNamespace(
        jcode_gateway_token="t",
        local_llm_enabled=True,
        local_models=["gpt-oss-120b", FLASH_ID],
        local_llm_url="http://gw/v1",
    )
    current = {"engine": "standard"}

    async def _load() -> str:
        return current["engine"]

    app.state.active_engine = engine.ActiveEngine(_load, ttl_s=0.0)
    c = TestClient(app)
    auth = {"Authorization": "Bearer t"}
    ids = [m["id"] for m in c.get("/api/jcode/llm/v1/models", headers=auth).json()["data"]]
    assert ids == ["gpt-oss-120b"]
    current["engine"] = "flash-next"
    ids = [m["id"] for m in c.get("/api/jcode/llm/v1/models", headers=auth).json()["data"]]
    assert ids == [FLASH_ID]
    # A name outside the catalog is still refused; a stale standard name is remapped (F3a,
    # covered in test_engine_switch.py).
    resp = c.post("/api/jcode/llm/v1/chat/completions", headers=auth, json={"model": "nope"})
    assert resp.status_code == 400


# --- launch-argument injection: both walls -----------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        # Flag smuggling: a second flag hidden inside an allowlisted flag's value.
        ["-ot", "x=CPU --rpc 10.0.0.1:50052"],
        ["-ub", "512 --rpc 10.0.0.1:50052"],
        # A newline/YAML break-out writing a second model with its own command.
        ["-ot", "x=CPU\n  evil:\n    cmd: sh -c id"],
        ["--load-mode", "mmap\n"],
        ["-ctk", "q8_0\tq4_0"],
        # The --ctx-checkpoints bound, bypassed by smuggling the flag into another value.
        ["-ot", "x=CPU --ctx-checkpoints 999"],
        ["--ctx-checkpoints", "8 --ctx-checkpoints 999"],
        # Quote/escape/comment characters the splitter or YAML would interpret.
        ["-ot", "x='CPU'"],
        ["-ot", "x=CPU#"],
        ["--spec-type", "draft-mtp;id"],
        # Numeric flags must be numbers.
        ["-ub", "lots"],
        ["--spec-draft-p-min", "1e9"],
        ["-ngl", "999x"],
        # -ot must be <pattern>=<buffer>.
        ["-ot", "per_layer_token_embd"],
        ["-ot", "a:b=CPU"],
    ],
)
def test_injected_launch_arguments_are_refused(args: list[str]) -> None:
    with pytest.raises(llm_settings.HTTPException) as err:
        llm_settings._validate_extra_args(args)
    assert err.value.status_code == 422


def test_the_vision_flash_attention_refusal_cannot_be_smuggled_past() -> None:
    vision = local_catalog.get("qwen3-vl-30b")
    with pytest.raises(llm_settings.HTTPException):
        llm_settings._validate_extra_args(["-ot", "x=CPU -fa 0"], vision)
    with pytest.raises(llm_settings.HTTPException):
        llm_settings._validate_extra_args(["-fa", "0"], vision)
    with pytest.raises(llm_settings.HTTPException):
        llm_settings._validate_extra_args(["-fa", "0 "], vision)


@pytest.mark.parametrize(
    "args",
    [
        ["-ot", "per_layer_token_embd=CPU"],
        ["--override-tensor", r"blk\.(1[0-9])\.ffn_.*_exps=CPU,per_layer_token_embd=CPU"],
        ["-ngl", "999", "-ub", "512", "--spec-draft-p-min", "0.6", "--load-mode", "mmap+mlock"],
        ["-ngl", "auto", "-fa", "on", "-ctk", "q8_0"],
    ],
)
def test_legitimate_launch_arguments_still_pass(args: list[str]) -> None:
    assert llm_settings._validate_extra_args(args) == args


@pytest.mark.parametrize(
    "bad",
    [
        "x=CPU --rpc 10.0.0.1:50052",
        "x=CPU\n  evil:\n    cmd: sh -c id",
        "a\x00b",
        "x='CPU'",
        "#",
        "",
    ],
)
def test_the_renderer_refuses_a_stored_unsafe_argument(tmp_path: Path, bad: str) -> None:
    """The second wall: an override stored before validation existed must not render — the
    model's operator flags are dropped and the reason reported, never smuggled into the YAML."""
    import yaml

    (tmp_path / "gpt-oss-120b").mkdir()
    (tmp_path / "gpt-oss-120b" / "m-mxfp4.gguf").write_bytes(b"\0")
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None
    rejected: dict[str, str] = {}
    text = llama_swap_config.render(
        [dataclasses.asdict(gpt)],
        str(tmp_path),
        extra_args={"gpt-oss-120b": ["-ot", bad]},
        rejected=rejected,
    )
    assert list(yaml.safe_load(text)["models"]) == ["gpt-oss-120b"]
    assert text == llama_swap_config.render([dataclasses.asdict(gpt)], str(tmp_path))
    assert set(rejected) == {"gpt-oss-120b"}


def test_an_empty_roster_renders_an_empty_mapping(tmp_path: Path) -> None:
    import yaml

    text = llama_swap_config.render([], str(tmp_path))
    assert yaml.safe_load(text)["models"] == {}
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None
    # A Flash-Next-only manifest leaves the standard file empty, not null.
    flash_only = llama_swap_config.render([dataclasses.asdict(_flash())], str(tmp_path))
    assert yaml.safe_load(flash_only)["models"] == {}


# --- the free-disk guard on install ------------------------------------------------------


def _install_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_gb: float) -> Any:
    monkeypatch.setattr(local_weights, "free_gb", lambda _d: free_gb)
    settings = _cloud_settings(
        local_llm_enabled=True, local_models=["gpt-oss-120b"], local_models_dir=str(tmp_path)
    )
    return _authed_client(settings)


def test_install_is_refused_when_the_models_volume_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, store = _install_client(tmp_path, monkeypatch, free_gb=60.0)
    resp = c.post(f"/api/settings/llm/local-models/{FLASH_ID}/install")
    assert resp.status_code == 409
    assert "not enough free disk" in resp.json()["detail"]
    assert FLASH_ID not in store.values.get("llm_local_provision_requested", [])


def test_install_is_allowed_with_room_and_counts_other_queued_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    need = _flash().size_gb + llm_settings.INSTALL_DISK_MARGIN_GB
    c, _ = _install_client(tmp_path, monkeypatch, free_gb=need + 1)
    assert c.post(f"/api/settings/llm/local-models/{FLASH_ID}/install").status_code == 200
    # The next queued download must fit beside the one already queued.
    resp = c.post("/api/settings/llm/local-models/llama-3.3-70b/install")
    assert resp.status_code == 409 and "other queued installs" in resp.json()["detail"]


def test_a_resumed_install_counts_the_bytes_already_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_weights, "dir_size_gb", lambda _d, mid: 80.0)
    c, _ = _install_client(tmp_path, monkeypatch, free_gb=20.0)
    assert c.post(f"/api/settings/llm/local-models/{FLASH_ID}/install").status_code == 200


def test_free_gb_measures_a_real_directory_and_skips_an_absent_one(tmp_path: Path) -> None:
    assert local_weights.free_gb("") is None
    assert local_weights.free_gb(str(tmp_path / "absent")) is None
    measured = local_weights.free_gb(str(tmp_path))
    assert measured is not None and measured >= 0.0


@pytest.mark.asyncio
async def test_props_carry_the_pool_beside_the_per_slot_n_ctx() -> None:
    """llama-server's `n_ctx` under `--kv-unified` is one slot's maximum (262144), not the pool;
    the debug read says so instead of leaving 262144 to be read as the window."""
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b", FLASH_ID])
    gw = FakeLocalGateway(props_payload={"n_ctx": 262144, "total_slots": 8})
    props = await llm_settings.gateway_props(FLASH_ID, settings, gw)  # type: ignore[arg-type]
    assert props["n_ctx"] == 262144
    pool = props["kv_pool"]
    assert isinstance(pool, dict) and pool["n_ctx"] == 524_288 and len(pool["slots"]) == 8
    gpt = await llm_settings.gateway_props("gpt-oss-120b", settings, gw)  # type: ignore[arg-type]
    assert "kv_pool" not in gpt


def test_tool_round_text_is_analysis_only_for_a_harmony_reasoner() -> None:
    """Flash-Next's `<think>` thinking is split onto its own channel, so its tool-round text is
    narration; gpt-oss's harmony content on a tool round is leaked analysis. A served name
    outside the catalog keeps the old harmony assumption."""
    assert not local_catalog.tool_round_text_is_analysis("qwen3.8-flash-next")
    assert local_catalog.tool_round_text_is_analysis("gpt-oss-120b")
    assert local_catalog.tool_round_text_is_analysis("not-in-the-catalog")
