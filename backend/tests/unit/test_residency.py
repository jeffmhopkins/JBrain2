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
    cloud = _settings(
        local_llm_enabled=False, local_llm_resident_group=True, local_models=models
    )
    assert default_resident_served(cloud) == []


def test_default_resident_served_only_counts_installed() -> None:
    s = _settings(
        local_llm_enabled=True, local_llm_resident_group=True, local_models=["gpt-oss-120b"]
    )
    # vl is recommended but not provisioned here → only the installed recommended model.
    assert default_resident_served(s) == ["gpt-oss-120b"]


@pytest.mark.asyncio
async def test_restore_loads_only_the_missing_hot_member() -> None:
    # The image render left 120b resident (the reply reloaded it) but vl cold — restore
    # loads exactly vl, never re-loading what's already hot.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = ResidencyCoordinator(gw, ["qwen3-vl-30b-a3b", "gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001 — exercising the unit of behavior directly
    assert gw.loaded == ["qwen3-vl-30b-a3b"]


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
