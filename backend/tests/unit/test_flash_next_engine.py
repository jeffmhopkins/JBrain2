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


def _recording_drop(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    dropped: list[str] = []

    def _drop(_dir: str, model_id: str) -> float:
        dropped.append(model_id)
        return 1.5

    monkeypatch.setattr(local_weights, "drop_weights_page_cache", _drop)
    return dropped


def test_the_drop_lever_keeps_a_resident_mapped_model_and_drops_it_once_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped = _recording_drop(monkeypatch)
    client = local_gateway.LocalGatewayClient("http://x/v1", models_dir=str(tmp_path))
    client._seen_resident = {FLASH_ID, "gpt-oss-120b"}
    freed = client.drop_page_cache([FLASH_ID, "gpt-oss-120b"])
    # Serving from that cache: left alone and NOT reported as a drop that freed nothing.
    assert dropped == ["gpt-oss-120b"] and FLASH_ID not in freed
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
    store.values["llm_local_engine_effective"] = "flash-next"
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    assert not (tmp_path / "llama-swap.yaml").exists()
    assert "--load-mode mmap" in (tmp_path / "llama-swap.flash-next.yaml").read_text()
    store.values["llm_local_engine_effective"] = "standard"
    await llm_settings.regen_gateway_config(settings, store)  # type: ignore[arg-type]
    assert flash.id not in (tmp_path / "llama-swap.yaml").read_text()


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
    save = " --slot-save-path /models/.kvslots/m"
    (tmp_path / "llama-swap.yaml").write_text(f"models:\n  m:\n    cmd: llama-server -c 1{save}\n")
    (tmp_path / "llama-swap.flash-next.yaml").write_text(
        f"models:\n  m:\n    cmd: llama-server -c 2{save}\n"
    )
    current = {"engine": "standard"}

    async def _load() -> str:
        return current["engine"]

    source = engine.ActiveEngine(_load, ttl_s=0.0)
    store = KvPrefixStore(object(), str(tmp_path), engine=source)  # type: ignore[arg-type]
    await store._refresh_engine()
    standard_fp = store.identity_of("m", "sys", [], None)
    current["engine"] = "flash-next"  # the debug route flips it on the live api
    await store._refresh_engine()
    flash_fp = store.identity_of("m", "sys", [], None)
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
    resp = c.post(
        "/api/jcode/llm/v1/chat/completions", headers=auth, json={"model": "gpt-oss-120b"}
    )
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
    """The second wall: an override stored before validation existed must not render."""
    from jbrain.llm import llama_swap_config

    (tmp_path / "gpt-oss-120b").mkdir()
    (tmp_path / "gpt-oss-120b" / "m-mxfp4.gguf").write_bytes(b"\0")
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None
    with pytest.raises(llama_swap_config.UnsafeArgument):
        llama_swap_config.render(
            [dataclasses.asdict(gpt)], str(tmp_path), extra_args={"gpt-oss-120b": ["-ot", bad]}
        )


def test_an_empty_roster_renders_an_empty_mapping(tmp_path: Path) -> None:
    import yaml

    from jbrain.llm import llama_swap_config

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
