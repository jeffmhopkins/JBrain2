"""Residency: the app as sole evictor (ensure_room) and the evict→restore cycle."""

import asyncio

import pytest

from jbrain.llm.residency import ResidencyCoordinator
from tests.unit.fakes import FakeLocalGateway

# Footprints at default windows (weights + KV) used by these tests, from the catalog:
#   gpt-oss-120b        = 59.0 + 4.5            = 63.5
#   qwen3-vl-30b-a3b    = 32.0 + 6.0*32768/128k = 33.5
#   qwen3-coder-next    = 49.6 + 5.0*262144/128k = 59.6
#   qwen3.5-4b          =  4.3 + 1.2*32768/128k =  4.6
#   qwen3.5-0.8b        =  0.9 + 0.5*32768/128k ≈  1.0  (the tiny model)


def _coord(
    gw: FakeLocalGateway,
    monkeypatch: pytest.MonkeyPatch,
    *,
    total: float,
    used: float,
    staged: list[str] | None = None,
    enabled: bool = True,
) -> ResidencyCoordinator:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (total, used)
    )

    async def _staged() -> list[str]:
        return staged or []

    return ResidencyCoordinator(
        gw,
        staged_loader=_staged if staged is not None else None,
        models_dir="",  # nominal catalog size_gb, no filesystem read
        enabled=enabled,
        free_ram_fraction=0.25,
    )


# --- ensure_room: evict the fewest to fit -----------------------------------


@pytest.mark.asyncio
async def test_ensure_room_evicts_the_big_model_and_keeps_the_tiny_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (63.5) + the tiny model resident, used=66; loading the coder (59.6) would blow
    # the 25% floor (ceiling 96 of 128). Evict the FEWEST to fit: drop gpt-oss alone (frees
    # enough), keep the tiny model — never evict the tiny one when a big one suffices.
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3.5-0.8b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=66.0)
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == ["gpt-oss-120b"]
    assert "qwen3.5-0.8b" in await gw.running()


@pytest.mark.asyncio
async def test_ensure_room_records_the_evicted_model_for_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The evicted model is remembered so the end-of-turn restore can bring it back.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0)
    await coord.ensure_room("qwen3-coder-next")  # 90+59.6 > 96 → evict gpt-oss
    assert gw.unloaded == ["gpt-oss-120b"]
    assert coord._displaced == {"gpt-oss-120b"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_room_evicts_nothing_when_it_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=40.0)
    await coord.ensure_room("qwen3.5-4b")  # 4.6 → 44.6, well under the 96 ceiling
    assert gw.unloaded == []
    assert coord._displaced == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_room_gives_a_too_big_model_the_whole_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model larger than the whole floor can't fit under it no matter what — so evict
    # EVERYTHING and load it anyway (the paradigm: load any model, unload until we can). On a
    # 64 GB box (ceiling 48) the coder (59.6) exceeds the floor even on a bare box.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=64.0, used=63.5)
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == ["gpt-oss-120b"]  # everything freed to make what room we can
    assert coord._displaced == {"gpt-oss-120b"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_room_is_a_noop_when_already_resident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=120.0)  # tight, but it's already up
    await coord.ensure_room("qwen3.5-4b")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_clears_a_loaded_model_from_the_displaced_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model displaced earlier is now being actively loaded again — it's no longer "pending
    # restore", so it drops out of the displaced set even though ensure_room evicts nothing.
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0)
    coord.note_evicted(["qwen3.5-4b"])
    assert coord._displaced == {"qwen3.5-4b"}  # noqa: SLF001
    await coord.ensure_room("qwen3.5-4b")
    assert coord._displaced == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_room_spares_staged_models_evicting_others_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss is STAGED (an explicit keep-hot pin), vl is not. Freeing room for a small model
    # evicts the non-staged vl and leaves the staged gpt-oss resident.
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3-vl-30b-a3b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=99.0, staged=["gpt-oss-120b"])
    await coord.ensure_room("qwen3.5-4b")
    assert gw.unloaded == ["qwen3-vl-30b-a3b"]
    assert "gpt-oss-120b" in await gw.running()


@pytest.mark.asyncio
async def test_ensure_room_is_a_noop_when_disabled() -> None:
    # enabled False (cloud-only box) → the app evicts nothing even with a full box (no
    # read_memory_gb call needed — it returns on the first line).
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3-vl-30b-a3b"})
    coord = ResidencyCoordinator(gw, enabled=False)
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_best_effort_when_memory_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    monkeypatch.setattr("jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": None)
    coord = ResidencyCoordinator(gw, models_dir="", enabled=True)
    await coord.ensure_room("qwen3-coder-next")  # can't measure RAM → evict nothing, no raise
    assert gw.unloaded == []


# --- note_evicted -----------------------------------------------------------


def test_note_evicted_records_only_known_catalog_models() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, enabled=True)
    coord.note_evicted(["gpt-oss-120b", "not-a-real-model"])
    assert coord._displaced == {"gpt-oss-120b"}  # noqa: SLF001 — the unknown name is ignored


def test_note_evicted_is_a_noop_when_disabled() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, enabled=False)
    coord.note_evicted(["gpt-oss-120b"])
    assert coord._displaced == set()  # noqa: SLF001


# --- restore: put the displaced (and staged) set back -----------------------


@pytest.mark.asyncio
async def test_restore_reloads_the_displaced_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # An image render / code session froze vl + gpt-oss (recorded via note_evicted); restore
    # loads them both back and clears the displaced set. 160 GB box (ceiling 120) so both
    # footprints (33.5 + 63.5 = 97, from used=10) fit.
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=160.0, used=10.0)
    coord.note_evicted(["qwen3-vl-30b-a3b", "gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001
    assert set(gw.loaded) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}
    assert coord._displaced == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_restore_reloads_the_staged_set_even_when_nothing_was_displaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staging keeps a model hot: a freshly staged, cold model is warmed by restore even with
    # an empty displaced set.
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0, staged=["qwen3.5-4b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == ["qwen3.5-4b"]


@pytest.mark.asyncio
async def test_restore_only_loads_members_that_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Opportunistic under the budget: gpt-oss (63.5) fits under the 96 ceiling from used=10,
    # but vl (33.5) would then push to 107 — so vl is left displaced to load on demand rather
    # than evicting a member to squeeze it in.
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0)
    coord.note_evicted(["gpt-oss-120b", "qwen3-vl-30b-a3b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == ["gpt-oss-120b"]
    assert coord._displaced == {"qwen3-vl-30b-a3b"}  # noqa: SLF001 — kept for a later restore


@pytest.mark.asyncio
async def test_restore_clears_a_member_that_came_back_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A displaced model that an on-demand request already reloaded is dropped from the set
    # (it's back) without a redundant load.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=63.5)
    coord.note_evicted(["gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == []
    assert coord._displaced == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_restore_is_a_noop_when_nothing_to_do() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, enabled=True)
    await coord._restore()  # noqa: SLF001 — empty displaced + no staged loader
    assert gw.loaded == []


@pytest.mark.asyncio
async def test_restore_suppresses_a_load_failure_and_clears_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gateway hiccup must never raise out of housekeeping; the attempted entry is cleared so
    # we don't spin retrying it every turn (a genuinely-wanted model re-displaces when evicted).
    gw = FakeLocalGateway(running=set(), fail_load=True)
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0)
    coord.note_evicted(["gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001
    assert coord._displaced == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_restore_is_a_noop_when_disabled() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, enabled=False)
    coord.note_evicted(["gpt-oss-120b"])  # no-op (disabled), so nothing to restore
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == []


# --- schedule_restore -------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_restore_warms_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=160.0, used=10.0)  # ceiling 120 — both members fit
    coord.note_evicted(["qwen3-vl-30b-a3b", "gpt-oss-120b"])
    coord.schedule_restore()
    await asyncio.gather(*list(coord._tasks))  # noqa: SLF001 — drain the fire-and-forget task
    assert set(gw.loaded) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}


@pytest.mark.asyncio
async def test_schedule_restore_coalesces_and_no_ops_on_empty_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0)
    coord.note_evicted(["qwen3-vl-30b-a3b"])
    coord.schedule_restore()
    coord.schedule_restore()  # one already in flight → dropped, not a second task
    assert len(coord._tasks) == 1  # noqa: SLF001
    await asyncio.gather(*list(coord._tasks))  # noqa: SLF001

    # Nothing displaced and no staged loader → never schedules anything.
    empty = ResidencyCoordinator(gw, enabled=True)
    empty.schedule_restore()
    assert empty._tasks == set()  # noqa: SLF001

    # Disabled box never schedules either.
    disabled = ResidencyCoordinator(gw, enabled=False)
    disabled.note_evicted(["gpt-oss-120b"])
    disabled.schedule_restore()
    assert disabled._tasks == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_evict_then_restore_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole cycle: a big load evicts a resident model (recorded), then once it's gone the
    # restore puts the evicted model back.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0)
    await coord.ensure_room("qwen3-coder-next")  # evicts gpt-oss, records it
    assert coord._displaced == {"gpt-oss-120b"}  # noqa: SLF001
    # The coder session ends and its model is gone; the box is empty again with room to spare.
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 10.0)
    )
    gw._running = set()  # noqa: SLF001 — the coder unloaded on power-off
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == ["gpt-oss-120b"]
    assert coord._displaced == set()  # noqa: SLF001
