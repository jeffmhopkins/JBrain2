"""Flash-Next F1 (docs/plans/FLASH_NEXT_ENGINE_PLAN.md): the catalog entry and its budget,
default slots, and engine separation on every surface that offers, loads or reads a model.

The config rendering itself is covered in test_llama_swap_config.py and the standard-file
regression in test_llama_swap_golden.py."""

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jbrain.api import llm_settings
from jbrain.llm import engine, local_catalog, local_gateway, local_weights, providers, smoketest
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError, ResidencyWiring
from tests.unit.fakes import FakeLocalGateway
from tests.unit.test_llm_settings_api import _authed_client, _cloud_settings

FLASH_ID = "qwen3.8-flash-next"


def _flash() -> local_catalog.LocalModel:
    model = local_catalog.get(FLASH_ID)
    assert model is not None
    return model


# --- the catalog entry ------------------------------------------------------------------


def test_the_entry_is_a_flash_next_engine_model_with_four_slots() -> None:
    m = _flash()
    assert m.engine == engine.FLASH_NEXT
    assert m.default_slots == 4
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
    assert m.file_backed_gb == pytest.approx(26.8)


def test_every_standard_entry_keeps_one_default_slot_and_no_file_backed_weights() -> None:
    for m in local_catalog.CATALOG:
        if m.engine == engine.STANDARD:
            assert m.default_slots == 1 and m.file_backed_gb == 0.0, m.id
            assert m.served_ctx_checkpoints == 0, m.id


def test_kv_term_is_the_plans_derivation() -> None:
    """§3: attention KV + QSA indexer, both q8_0, per 128k tokens per slot."""
    per_token = 12 * 2 * 256 * 2 * 1.0625 + 12 * 256 * 1.0625
    assert _flash().kv_gb_per_128k == pytest.approx(per_token * 131072 / 1024**3, abs=0.01)


def test_checkpoints_are_pinned_at_eight_per_slot() -> None:
    m = _flash()
    assert local_catalog.ctx_checkpoints(m.checkpoint_gb, m.served_ctx_checkpoints) == 8
    # Without the pin, the measured-cost rule would hand a derived figure 16.
    assert local_catalog.ctx_checkpoints(m.checkpoint_gb) == local_catalog.CTX_CHECKPOINTS


def test_resident_footprint_is_the_plans_83_gib_at_four_full_slots() -> None:
    m = _flash()
    total = local_catalog.footprint_gb(m, 262144)
    assert total == pytest.approx(83, abs=1.0)
    # The parts, so a drift is attributable: weights less the engram table, 4 x 262k of KV,
    # 32 checkpoints, the flat overhead and the vision workspace.
    expected = (
        (m.size_gb - m.file_backed_gb)
        + m.kv_gb_per_128k * 2 * 4
        + 0.11 * 8 * 4
        + m.runtime_overhead_gb  # type: ignore[operator]
        + local_catalog.vision_attn_buffer_gb()
    )
    assert total == pytest.approx(expected, abs=0.02)


def test_file_backed_weights_are_subtracted_from_the_measured_disk_size() -> None:
    m = _flash()
    at_disk = local_catalog.footprint_gb(m, 262144, disk_gb=90.0)
    # Every GB on disk above the file-backed share is a resident GB; the share itself is not.
    assert at_disk - local_catalog.footprint_gb(m, 262144, disk_gb=89.0) == pytest.approx(1.0)
    no_backing = dataclasses.replace(m, file_backed_gb=0.0)
    assert at_disk == pytest.approx(
        local_catalog.footprint_gb(no_backing, 262144, disk_gb=90.0 - 26.8), abs=0.01
    )
    # A partial download can never drive the weights term negative.
    assert local_catalog.footprint_gb(m, 262144, disk_gb=1.0) == local_catalog.footprint_gb(
        m, 262144, disk_gb=0.0
    )
    host, device = local_catalog.declared_gb(m, 262144, disk_gb=90.0)
    assert host == pytest.approx(at_disk, abs=0.02)
    assert host - device == pytest.approx(0.11 * 8 * 4, abs=0.02)
    assert local_catalog.load_footprint_gb(m) < m.size_gb + m.kv_gb_per_128k * 8


def test_footprint_defaults_to_the_catalog_slot_count() -> None:
    m = _flash()
    assert local_catalog.footprint_gb(m, 262144) == local_catalog.footprint_gb(m, 262144, slots=4)
    two = local_catalog.footprint_gb(m, 262144, slots=2)
    assert local_catalog.footprint_gb(m, 262144) - two == pytest.approx(
        m.kv_gb_per_128k * 2 * 2 + 0.11 * 8 * 2, abs=0.02
    )
    assert m.served_slots({}) == 4 and m.served_slots({FLASH_ID: 2}) == 2
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None and gpt.served_slots({}) == 1


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
    with pytest.raises(ResidencyError, match="flash-next engine"):
        await coord.ensure_room(FLASH_ID)
    with pytest.raises(ResidencyError, match="flash-next engine"):
        await coord.free_room(FLASH_ID)
    # Refused BEFORE any eviction: the resident model was not touched for a load that fails.
    assert gw.unloaded == []
    with pytest.raises(ResidencyError, match="standard engine"):
        await _coord(engine.FLASH_NEXT, gw).ensure_room("gpt-oss-120b")


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
    assert fp == local_catalog.footprint_gb(_flash(), 262144, slots=4)


@pytest.mark.asyncio
async def test_gateway_served_shape_defaults_to_the_catalog_slots() -> None:
    client = local_gateway.LocalGatewayClient("http://x/v1")
    assert await client._served_shape(_flash()) == (262144, 4)

    async def _none() -> dict[str, int]:
        return {}

    wired = local_gateway.LocalGatewayClient("http://x/v1", slots_loader=_none)
    assert await wired._served_shape(_flash()) == (262144, 4)


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
    save = " --slot-save-path /models/.kvslots/m"
    (tmp_path / "llama-swap.yaml").write_text(f"models:\n  m:\n    cmd: llama-server -c 1{save}\n")
    flash_cfg = tmp_path / "llama-swap.flash-next.yaml"
    flash_cfg.write_text(f"models:\n  m:\n    cmd: llama-server -c 2{save}\n")
    store = KvPrefixStore(object(), str(tmp_path))  # type: ignore[arg-type]
    standard_fp = store.identity_of("m", "sys", [], None)
    store.set_engine(engine.FLASH_NEXT)
    flash_fp = store.identity_of("m", "sys", [], None)
    assert standard_fp is not None and flash_fp is not None and standard_fp != flash_fp
    built = KvPrefixStore(object(), str(tmp_path), engine=engine.FLASH_NEXT)  # type: ignore[arg-type]
    assert built.identity_of("m", "sys", [], None) == flash_fp
    flash_cfg.unlink()
    assert store.identity_of("m", "sys", [], None) is None


# --- the page-cache drop -----------------------------------------------------------------


def test_the_page_cache_drop_skips_a_file_backed_model(tmp_path: Path) -> None:
    (tmp_path / FLASH_ID).mkdir()
    (tmp_path / FLASH_ID / "w.gguf").write_bytes(b"\0" * 4096)
    calls: list[str] = []
    real = local_weights._drop_page_cache

    def _spy(root: str, suffixes: tuple[str, ...]) -> float | None:
        calls.append(root)
        return real(root, suffixes)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(local_weights, "_drop_page_cache", _spy)
        assert local_weights.drop_weights_page_cache(str(tmp_path), FLASH_ID) == 0.0
        assert calls == []
        local_weights.drop_weights_page_cache(str(tmp_path), "gpt-oss-120b")
        assert calls == [str(tmp_path / "gpt-oss-120b")]
    assert local_weights.serves_file_backed(FLASH_ID)
    assert not local_weights.serves_file_backed("gpt-oss-120b")


def test_the_gateway_keeps_the_engram_cache_after_a_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list[str] = []
    monkeypatch.setattr(
        local_weights, "drop_weights_page_cache", lambda _d, mid: dropped.append(mid) or 0.0
    )
    client = local_gateway.LocalGatewayClient("http://x/v1", models_dir=str(tmp_path))
    client._drop_weights_cache(_flash())
    assert dropped == []
    gpt = local_catalog.get("gpt-oss-120b")
    client._drop_weights_cache(gpt)
    assert dropped == ["gpt-oss-120b"]


# --- the settings API --------------------------------------------------------------------


def _api(engine_now: str | None = None) -> Any:
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b", FLASH_ID])
    c, store = _authed_client(settings)
    if engine_now is not None:
        store.values["llm_local_engine"] = engine_now
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
    assert row["parallel_slots"] == 4 and row["parallel_slots_max"] == 4
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
    assert llm_settings.slots_max(_flash()) == 4
    c, store = _api()
    url = f"/api/settings/llm/local-models/{FLASH_ID}/parallel-slots"
    assert c.put(url, json={"slots": 5}).status_code == 422
    # 1 would be stored as an absence and read back as the default 4, so it is refused.
    assert c.put(url, json={"slots": 1}).status_code == 422
    resp = c.put(url, json={"slots": 3})
    assert resp.status_code == 200
    assert {m["id"]: m for m in resp.json()["local_models"]}[FLASH_ID]["parallel_slots"] == 3
    resp = c.put(url, json={"slots": None})
    assert {m["id"]: m for m in resp.json()["local_models"]}[FLASH_ID]["parallel_slots"] == 4
    assert (
        c.put(
            "/api/settings/llm/local-models/gpt-oss-120b/parallel-slots", json={"slots": 3}
        ).status_code
        == 422
    )


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
    store.values["llm_local_engine"] = "flash-next"
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    assert not (tmp_path / "llama-swap.yaml").exists()
    assert "--load-mode mmap" in (tmp_path / "llama-swap.flash-next.yaml").read_text()
    store.values["llm_local_engine"] = "standard"
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    assert flash.id not in (tmp_path / "llama-swap.yaml").read_text()
