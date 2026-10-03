"""Flash-Next's pool size, chosen without a release: 512k by default, 1M on request.

The size rides in the per-model context-window override map (a pooled model has no other use
for it), so every reader that already prices the saved window — render, the load charge,
residency, the settings meter, the pool guard — must agree on the size actually served.
"""

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from jbrain.api import llm_settings
from jbrain.llm import engine as engines
from jbrain.llm import llama_swap_config, local_catalog, slot_roles
from jbrain.llm.kv_pool_guard import KvPoolGuard
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, SlotRole
from tests.unit.fakes import FakeLocalGateway
from tests.unit.test_llm_settings_api import _authed_client, _cloud_settings

FLASH_ID = "qwen3.8-flash-next"
ONE_M = 1_048_576
HALF_M = 524_288


def _flash() -> local_catalog.LocalModel:
    model = local_catalog.get(FLASH_ID)
    assert model is not None
    return model


# --- the pool and the catalog --------------------------------------------------------------


def test_the_default_stays_512k_and_1m_is_the_one_alternative() -> None:
    assert FLASH_NEXT_POOL.n_ctx == HALF_M
    assert FLASH_NEXT_POOL.cell_choices == (HALF_M, ONE_M)


def test_resized_honours_only_a_listed_size() -> None:
    assert FLASH_NEXT_POOL.resized(ONE_M).n_ctx == ONE_M
    # Roles, caps and slots are the pool's, whatever its size.
    assert FLASH_NEXT_POOL.resized(ONE_M).reservations == FLASH_NEXT_POOL.reservations
    for other in (None, HALF_M, 262_144, 65_536, 2 * ONE_M):
        assert FLASH_NEXT_POOL.resized(other) is FLASH_NEXT_POOL


def test_a_default_outside_its_choices_is_refused() -> None:
    with pytest.raises(ValueError, match="one of its choices"):
        dataclasses.replace(FLASH_NEXT_POOL, n_ctx=262_144)


def test_effective_pool_reads_the_saved_size() -> None:
    m = _flash()
    assert local_catalog.effective_pool(m, None) is FLASH_NEXT_POOL
    assert local_catalog.effective_pool(m, {FLASH_ID: 65_536}) is FLASH_NEXT_POOL
    pool = local_catalog.effective_pool(m, {FLASH_ID: ONE_M})
    assert pool is not None and pool.n_ctx == ONE_M
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None and local_catalog.effective_pool(gpt, {"gpt-oss-120b": 1}) is None


def test_the_footprint_charges_the_saved_pool_size() -> None:
    """3.5 GiB per 128k: the 1M pool books 14 GiB more KV than 512k, on every reader."""
    m = _flash()
    small = local_catalog.footprint_gb(m, m.context_window)
    big = local_catalog.footprint_gb(m, ONE_M)
    assert big - small == pytest.approx(14.0, abs=0.02)
    assert local_catalog.declared_gb(m, ONE_M)[1] - local_catalog.declared_gb(m, HALF_M)[
        1
    ] == pytest.approx(14.0, abs=0.02)
    assert local_catalog.load_footprint_gb(m, ONE_M) > local_catalog.load_footprint_gb(m)


# --- the rendered command ------------------------------------------------------------------


def _render(tmp_path: Path, windows: dict[str, int]) -> str:
    root = tmp_path / FLASH_ID
    root.mkdir(exist_ok=True)
    (root / "Qwen3.8-Flash-Next-UD-IQ4_XS.gguf").write_bytes(b"\0")
    (root / "mmproj-F16.gguf").write_bytes(b"\0")
    manifest = [dataclasses.asdict(_flash())]
    return llama_swap_config.render(
        manifest, str(tmp_path), windows=windows, engine=engines.FLASH_NEXT
    )


def test_render_stamps_the_saved_pool_size(tmp_path: Path) -> None:
    assert f" -c {HALF_M} " in _render(tmp_path, {})
    assert f" -c {ONE_M} " in _render(tmp_path, {FLASH_ID: ONE_M})
    # A stale per-sequence override is still ignored.
    assert f" -c {HALF_M} " in _render(tmp_path, {FLASH_ID: 65_536})
    assert " -np 8 --kv-unified " in _render(tmp_path, {FLASH_ID: ONE_M})


def test_flash_next_loads_with_load_mode_none_and_keeps_the_engram_lazy_on_cpu(
    tmp_path: Path,
) -> None:
    text = _render(tmp_path, {})
    assert "--load-mode none" in text and "--load-mode mmap" not in text
    assert "--no-mmap" not in text
    assert "-ot per_layer_token_embd=CPU" in text and "--lazy-mode on" in text


def test_pool_shape_honours_only_a_listed_saved_size() -> None:
    manifest = dataclasses.asdict(_flash())
    assert slot_roles.pool_shape(manifest) == (HALF_M, 8)
    assert slot_roles.pool_shape(manifest, ONE_M) == (ONE_M, 8)
    assert slot_roles.pool_shape(manifest, 131_072) == (HALF_M, 8)


# --- the settings routes -------------------------------------------------------------------


def _api(gateway: FakeLocalGateway | None = None) -> Any:
    settings = _cloud_settings(local_llm_enabled=True, local_models=["gpt-oss-120b", FLASH_ID])
    c, store = _authed_client(settings, gateway)
    store.values["llm_local_engine"] = "flash-next"
    store.values["llm_local_engine_effective"] = "flash-next"
    return c, store


def _row(body: dict[str, Any]) -> dict[str, Any]:
    return {m["id"]: m for m in body["local_models"]}[FLASH_ID]


def test_the_owner_route_sets_1m_and_reverts_to_512k() -> None:
    gw = FakeLocalGateway(running={FLASH_ID})
    c, store = _api(gw)
    url = f"/api/settings/llm/local-models/{FLASH_ID}/context-window"
    before = _row(c.get("/api/settings/llm").json())
    resp = c.put(url, json={"context_window": ONE_M})
    assert resp.status_code == 200, resp.text
    assert store.values["llm_local_context_windows"] == {FLASH_ID: ONE_M}
    row = _row(resp.json())
    assert row["kv_pool"]["n_ctx"] == ONE_M and len(row["kv_pool"]["slots"]) == 8
    assert row["kv_gb"] == pytest.approx(before["kv_gb"] + 14.0, abs=0.05)
    # The served pool is allocated at load, so a resident model is unloaded to re-stamp it.
    assert gw.unloaded == [FLASH_ID]

    resp = c.put(url, json={"context_window": HALF_M})
    assert resp.status_code == 200
    assert store.values["llm_local_context_windows"] == {}
    assert _row(resp.json())["kv_pool"]["n_ctx"] == HALF_M


def test_setting_the_size_already_served_does_not_unload() -> None:
    gw = FakeLocalGateway(running={FLASH_ID})
    c, store = _api(gw)
    url = f"/api/settings/llm/local-models/{FLASH_ID}/context-window"
    assert c.put(url, json={"context_window": HALF_M}).status_code == 200
    assert c.put(url, json={"context_window": None}).status_code == 200
    assert gw.unloaded == []
    store.values["llm_local_context_windows"] = {FLASH_ID: ONE_M}
    assert c.put(url, json={"context_window": ONE_M}).status_code == 200
    assert gw.unloaded == []
    # Clearing a chosen 1M back to the default IS a change of served size.
    assert c.put(url, json={"context_window": None}).status_code == 200
    assert store.values["llm_local_context_windows"] == {}
    assert gw.unloaded == [FLASH_ID]


@pytest.mark.parametrize("window", [65_536, 131_072, 786_432, 2 * ONE_M])
def test_any_other_size_is_still_a_409_naming_the_choices(window: int) -> None:
    c, store = _api()
    resp = c.put(
        f"/api/settings/llm/local-models/{FLASH_ID}/context-window",
        json={"context_window": window},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "context window is fixed" in detail and "524,288 or 1,048,576" in detail
    assert FLASH_ID not in store.values.get("llm_local_context_windows", {})


def test_a_standard_model_still_rejects_a_pool_sized_window_by_range() -> None:
    c, _ = _api()
    resp = c.put(
        "/api/settings/llm/local-models/gpt-oss-120b/context-window",
        json={"context_window": ONE_M},
    )
    assert resp.status_code == 422


async def test_props_report_the_saved_pool_size() -> None:
    settings = _cloud_settings(local_llm_enabled=True, local_models=[FLASH_ID])
    gw = FakeLocalGateway(props_payload={"n_ctx": 262144, "total_slots": 8})
    props = await llm_settings.gateway_props(
        FLASH_ID,
        settings,
        gw,  # type: ignore[arg-type]
        {FLASH_ID: ONE_M},
    )
    pool = props["kv_pool"]
    assert isinstance(pool, dict) and pool["n_ctx"] == ONE_M


# --- the pool guard ------------------------------------------------------------------------


def _slots(held: int, cleared: frozenset[int] = frozenset()) -> list[dict[str, object]]:
    return [
        {
            "id": i,
            "n_ctx": 262_144,
            "is_processing": False,
            "n_prompt_tokens": held if i and i not in cleared else 0,
            "next_token": [{"n_remain": 0, "n_decoded": 0}],
        }
        for i in range(8)
    ]


async def _erased_for(windows: dict[str, int] | None) -> list[int]:
    """Seven idle slots hold 100k each (700k): under a 1M pool a 200k call fits as is, under
    512k the guard must free slots first."""
    erased: list[int] = []

    async def read(_model: str) -> list[dict[str, object]]:
        return _slots(100_000, frozenset(erased))

    async def erase(_model: str, slot: int) -> bool:
        erased.append(slot)
        return True

    async def loader() -> dict[str, int]:
        assert windows is not None
        return windows

    guard = KvPoolGuard(
        read,
        erase,
        wait_s=0.01,
        poll_s=0.001,
        windows_loader=loader if windows is not None else None,
    )
    async with guard.placed(
        FLASH_ID,
        FLASH_NEXT_POOL,
        SlotRole.INTERACTIVE,
        prompt_tokens=190_000,
        max_tokens=10_000,
    ):
        pass
    return erased


async def test_the_pool_guard_holds_calls_to_the_served_size() -> None:
    assert await _erased_for({FLASH_ID: ONE_M}) == []
    assert await _erased_for({}) != []
    assert await _erased_for(None) != []


async def test_an_unreadable_size_keeps_the_smaller_default() -> None:
    async def read(_model: str) -> list[dict[str, object]]:
        return _slots(0)

    async def erase(_model: str, _slot: int) -> bool:
        return True

    async def broken() -> dict[str, int]:
        raise RuntimeError("settings down")

    guard = KvPoolGuard(read, erase, windows_loader=broken)
    assert await guard._sized(FLASH_ID, FLASH_NEXT_POOL) is FLASH_NEXT_POOL


async def test_the_size_read_is_cached_briefly() -> None:
    reads: list[int] = []
    now = [0.0]

    async def read(_model: str) -> list[dict[str, object]]:
        return _slots(0)

    async def erase(_model: str, _slot: int) -> bool:
        return True

    async def loader() -> dict[str, int]:
        reads.append(1)
        return {FLASH_ID: ONE_M}

    guard = KvPoolGuard(read, erase, windows_loader=loader, clock=lambda: now[0])
    for _ in range(3):
        assert (await guard._sized(FLASH_ID, FLASH_NEXT_POOL)).n_ctx == ONE_M
    assert len(reads) == 1
    now[0] += 11.0
    await guard._sized(FLASH_ID, FLASH_NEXT_POOL)
    assert len(reads) == 2


# --- a resize whose unload fails -----------------------------------------------------------


def test_a_failed_unload_restores_the_previous_size_and_says_so() -> None:
    """A resident pool left at its old size while the budget prices the new one is 14 GiB
    wrong; the change is undone instead."""
    gw = FakeLocalGateway(running={FLASH_ID})
    gw.fail_unload = True
    c, store = _api(gw)
    store.values["llm_local_context_windows"] = {"gpt-oss-120b": 65536}
    resp = c.put(
        f"/api/settings/llm/local-models/{FLASH_ID}/context-window",
        json={"context_window": ONE_M},
    )
    assert resp.status_code == 502
    assert "could not be unloaded" in resp.json()["detail"]
    assert "524,288" in resp.json()["detail"]
    assert store.values["llm_local_context_windows"] == {"gpt-oss-120b": 65536}


def test_an_unreadable_loaded_list_restores_the_previous_size() -> None:
    """A failed residency read is not "nothing loaded": saving the new size over a model that
    may be resident at the old one would leave the budget 14 GiB wrong."""
    gw = FakeLocalGateway(running={FLASH_ID})

    async def unreadable() -> None:
        return None

    gw.running_states = unreadable  # type: ignore[method-assign]
    c, store = _api(gw)
    store.values["llm_local_context_windows"] = {"gpt-oss-120b": 65536}
    resp = c.put(
        f"/api/settings/llm/local-models/{FLASH_ID}/context-window",
        json={"context_window": ONE_M},
    )
    assert resp.status_code == 502
    assert "could not be read" in resp.json()["detail"]
    assert store.values["llm_local_context_windows"] == {"gpt-oss-120b": 65536}
    assert gw.unloaded == []


# --- the update smoketest ------------------------------------------------------------------


def test_the_rendered_config_reports_the_served_pool(tmp_path: Path) -> None:
    root = tmp_path / FLASH_ID
    root.mkdir()
    (root / "Qwen3.8-Flash-Next-UD-IQ4_XS.gguf").write_bytes(b"\0")
    (root / "mmproj-F16.gguf").write_bytes(b"\0")
    manifest = [dataclasses.asdict(_flash())]
    llama_swap_config.write(
        str(tmp_path), manifest, windows={FLASH_ID: ONE_M}, engine=engines.FLASH_NEXT
    )
    assert llama_swap_config.served_pool_cells_from_config(str(tmp_path), engines.FLASH_NEXT) == {
        FLASH_ID: ONE_M
    }
    # The per-sequence shape is unchanged: a slot still cannot pass the training length.
    assert llama_swap_config.served_shape_from_config(str(tmp_path), engines.FLASH_NEXT) == {
        FLASH_ID: (262_144, 8)
    }
    assert llama_swap_config.served_pool_cells_from_config(str(tmp_path / "nope")) == {}


def test_the_smoketest_gates_a_pooled_model_at_its_served_pool() -> None:
    from jbrain.llm import smoketest

    m = _flash()
    default = smoketest._resident_cost_gb(m)
    assert smoketest._resident_cost_gb(m, {FLASH_ID: ONE_M}) == pytest.approx(
        default + 14.0, abs=0.05
    )
    assert smoketest._resident_cost_gb(m, {"other": ONE_M}) == default
