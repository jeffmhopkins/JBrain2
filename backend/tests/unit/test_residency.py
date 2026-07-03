"""End-of-turn residency: the hot-set computation and the eager re-warm."""

import asyncio

import pytest

from jbrain.config import Settings
from jbrain.llm.residency import ResidencyCoordinator, default_resident_served
from tests.unit.fakes import FakeLocalGateway

_DB = "postgresql+asyncpg://nobody@localhost:1/none"


def _settings(**kw: object) -> Settings:
    return Settings(database_url=_DB, secure_cookies=False, **kw)  # type: ignore[arg-type]


def test_default_resident_served_is_recommended_provisioned_set() -> None:
    s = _settings(
        local_llm_enabled=True,
        local_llm_resident_group=True,
        local_models=["qwen3-vl-30b", "gpt-oss-120b", "qwen3-coder-next"],
    )
    served = default_resident_served(s)
    # The recommended pair only — the coder is provisioned but not recommended, so it is
    # not part of the box's baseline hot set (it gets the box to itself when in use).
    assert set(served) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}


def test_default_resident_served_empty_when_opted_out_or_disabled() -> None:
    models = ["qwen3-vl-30b", "gpt-oss-120b"]
    # Co-residency opted out → no baseline set (the recommended models swap one at a time).
    opted_out = _settings(
        local_llm_enabled=True, local_llm_resident_group=False, local_models=models
    )
    assert default_resident_served(opted_out) == []
    # Local hosting off (cloud-only box) → nothing to keep resident.
    cloud = _settings(local_llm_enabled=False, local_llm_resident_group=True, local_models=models)
    assert default_resident_served(cloud) == []


def test_default_resident_served_only_counts_installed() -> None:
    s = _settings(
        local_llm_enabled=True, local_llm_resident_group=True, local_models=["gpt-oss-120b"]
    )
    # vl is recommended but not provisioned here → only the installed recommended model.
    assert default_resident_served(s) == ["gpt-oss-120b"]


def test_co_residency_is_off_by_default() -> None:
    # The default is OPT-IN: co-residency pins ~91 GB and hard-froze the box, so with the
    # flag unset the recommended set is NOT kept co-resident even when both models are
    # provisioned. This is what makes a routine update stop the dual-load with no .env edit.
    s = _settings(local_llm_enabled=True, local_models=["qwen3-vl-30b", "gpt-oss-120b"])
    assert s.local_llm_resident_group is False
    assert default_resident_served(s) == []


@pytest.mark.asyncio
async def test_restore_loads_only_the_missing_hot_member() -> None:
    # The image render left 120b resident (the reply reloaded it) but vl cold — restore
    # loads exactly vl, never re-loading what's already hot.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b", "gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001 — exercising the unit of behavior directly
    assert gw.loaded == ["qwen3-vl-30b-a3b"]


@pytest.mark.asyncio
async def test_restore_re_warms_the_staged_set_even_with_no_recommended() -> None:
    # The owner pinned the pair by STAGING (not the env flag), so the recommended set is
    # empty but the staged ids still define the hot set — restore must honor them.
    gw = FakeLocalGateway(running=set())

    async def staged() -> list[str]:
        return ["qwen3-vl-30b", "gpt-oss-120b"]  # catalog ids → served names via the catalog

    coord = ResidencyCoordinator(gw, [], staged_loader=staged)
    await coord._restore()  # noqa: SLF001
    assert set(gw.loaded) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}


@pytest.mark.asyncio
async def test_restore_unions_recommended_and_staged_without_dupes() -> None:
    gw = FakeLocalGateway(running=set())

    async def staged() -> list[str]:
        return ["gpt-oss-120b", "qwen3-coder-next"]  # 120b overlaps the recommended set

    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b", "gpt-oss-120b"], staged_loader=staged)
    await coord._restore()  # noqa: SLF001
    assert sorted(gw.loaded) == ["gpt-oss-120b", "qwen3-coder-next", "qwen3-vl-30b-a3b"]


@pytest.mark.asyncio
async def test_restore_degrades_to_recommended_when_staged_load_fails() -> None:
    gw = FakeLocalGateway(running=set())

    async def staged() -> list[str]:
        raise RuntimeError("settings store unreachable")

    coord = ResidencyCoordinator(gw, ["gpt-oss-120b"], staged_loader=staged)
    await coord._restore()  # noqa: SLF001 — a staged-load failure must not break housekeeping
    assert gw.loaded == ["gpt-oss-120b"]


@pytest.mark.asyncio
async def test_restore_is_a_noop_when_set_already_hot() -> None:
    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b", "gpt-oss-120b"})
    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b", "gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == []


@pytest.mark.asyncio
async def test_restore_suppresses_a_load_failure() -> None:
    gw = FakeLocalGateway(running=set(), fail_load=True)
    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b"])
    await coord._restore()  # noqa: SLF001 — a gateway hiccup must never raise out of housekeeping


@pytest.mark.asyncio
async def test_schedule_restore_warms_in_the_background() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b", "gpt-oss-120b"])
    coord.schedule_restore()
    await asyncio.gather(*list(coord._tasks))  # noqa: SLF001 — drain the fire-and-forget task
    assert set(gw.loaded) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}


# --- memory-budgeted co-residency (ensure_room + budgeted re-warm) -----------
# Footprints at default windows (weights + KV) used by these tests, from the catalog:
#   gpt-oss-120b        = 59.0 + 4.5           = 63.5
#   qwen3-vl-30b-a3b    = 32.0 + 6.0*32768/128k = 33.5
#   qwen3-coder-next    = 49.6 + 5.0*262144/128k = 59.6
#   qwen3.5-4b          =  4.3 + 1.2*32768/128k =  4.6
#   qwen3.5-0.8b        =  0.9 + 0.5*32768/128k ≈  1.0  (the tiny model)


def _budgeted(
    gw: FakeLocalGateway,
    monkeypatch: pytest.MonkeyPatch,
    *,
    total: float,
    used: float,
    recommended: list[str] | None = None,
    staged: list[str] | None = None,
) -> ResidencyCoordinator:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (total, used)
    )

    async def _staged() -> list[str]:
        return staged or []

    return ResidencyCoordinator(
        gw,
        recommended or [],
        staged_loader=_staged if staged is not None else None,
        models_dir="",  # nominal catalog size_gb, no filesystem read
        budget_enabled=True,
        free_ram_fraction=0.25,
    )


@pytest.mark.asyncio
async def test_ensure_room_evicts_the_big_model_and_keeps_the_tiny_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (63.5) + the tiny model resident, used=66; loading the coder (59.6) would
    # blow the 25% floor (ceiling 96 of 128). Evict the FEWEST to fit: drop gpt-oss alone
    # (frees enough), keep the tiny model — never evict the tiny one when a big one suffices.
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3.5-0.8b"})
    coord = _budgeted(gw, monkeypatch, total=128.0, used=66.0)
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == ["gpt-oss-120b"]
    assert "qwen3.5-0.8b" in await gw.running()


@pytest.mark.asyncio
async def test_ensure_room_evicts_nothing_when_it_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _budgeted(gw, monkeypatch, total=128.0, used=40.0)
    await coord.ensure_room("qwen3.5-4b")  # 4.6 → 44.6, well under the 96 ceiling
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_is_a_noop_when_already_resident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    coord = _budgeted(gw, monkeypatch, total=128.0, used=120.0)  # tight, but it's already up
    await coord.ensure_room("qwen3.5-4b")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_spares_staged_models_evicting_others_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss is STAGED (an explicit keep-hot pin), vl is not. Freeing room for a small
    # model evicts the non-staged vl and leaves the staged gpt-oss resident.
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3-vl-30b-a3b"})
    coord = _budgeted(gw, monkeypatch, total=128.0, used=99.0, staged=["gpt-oss-120b"])
    await coord.ensure_room("qwen3.5-4b")
    assert gw.unloaded == ["qwen3-vl-30b-a3b"]
    assert "gpt-oss-120b" in await gw.running()


@pytest.mark.asyncio
async def test_ensure_room_is_a_noop_when_co_residency_off() -> None:
    # budget_enabled False → the gateway swaps on its own; the app evicts nothing even
    # with a full box (no read_memory_gb call needed — it returns on the first line).
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3-vl-30b-a3b"})
    coord = ResidencyCoordinator(gw, [], budget_enabled=False)
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_best_effort_when_memory_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    monkeypatch.setattr("jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": None)
    coord = ResidencyCoordinator(gw, [], models_dir="", budget_enabled=True)
    await coord.ensure_room("qwen3-coder-next")  # can't measure RAM → evict nothing, no raise
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_budgeted_restore_only_loads_members_that_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-warm is opportunistic under the budget: gpt-oss (63.5) fits under the 96 ceiling
    # from a used=10 baseline, but vl (33.5) would push to 107 — so it's left to load on
    # demand rather than evicting a hot member to squeeze it in.
    gw = FakeLocalGateway(running=set())
    coord = _budgeted(
        gw, monkeypatch, total=128.0, used=10.0, recommended=["gpt-oss-120b", "qwen3-vl-30b-a3b"]
    )
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == ["gpt-oss-120b"]


@pytest.mark.asyncio
async def test_schedule_restore_coalesces_and_no_ops_on_empty_set() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b"])
    coord.schedule_restore()
    coord.schedule_restore()  # one already in flight → dropped, not a second task
    assert len(coord._tasks) == 1  # noqa: SLF001
    await asyncio.gather(*list(coord._tasks))  # noqa: SLF001

    # Empty hot set never schedules anything.
    empty = ResidencyCoordinator(gw, [])
    empty.schedule_restore()
    assert empty._tasks == set()  # noqa: SLF001
