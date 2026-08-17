"""Residency: the app as sole evictor (ensure_room) and the evict→restore cycle."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from jbrain.llm.residency import ResidencyCoordinator, ResidencyError
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
    enabled: bool = True,
    fraction_loader: object = None,
    hold_loader: object = None,
    slots_loader: object = None,
    on_prefix_lost: object = None,
) -> ResidencyCoordinator:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (total, used)
    )
    return ResidencyCoordinator(
        gw,
        models_dir="",  # nominal catalog size_gb, no filesystem read
        enabled=enabled,
        free_ram_fraction=0.25,
        fraction_loader=fraction_loader,  # type: ignore[arg-type]
        hold_loader=hold_loader,  # type: ignore[arg-type]
        slots_loader=slots_loader,  # type: ignore[arg-type]
        on_prefix_lost=on_prefix_lost,  # type: ignore[arg-type]
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


# --- code-mode box reservation (hold_loader) --------------------------------


async def _held(*names: str) -> frozenset[str]:
    return frozenset(n for n in names if n)


@pytest.mark.asyncio
async def test_hold_refuses_a_non_reserved_load_and_evicts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # While code mode holds the box, a load of a model OUTSIDE its reserved set (and not
    # resident) is refused with ResidencyError and evicts NOTHING — nothing pulls a reserved
    # model out or co-loads a second large model past physical RAM (the OOM this guards).
    gw = FakeLocalGateway(running={"qwen3-coder-next"})
    coord = _coord(
        gw, monkeypatch, total=128.0, used=59.6, hold_loader=lambda: _held("qwen3-coder-next")
    )
    with pytest.raises(ResidencyError):
        await coord.ensure_room("gpt-oss-120b")
    assert gw.unloaded == []  # the coder was never evicted


@pytest.mark.asyncio
async def test_hold_allows_the_reserved_coder_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reserved model is exempt — it must be able to (re)load while the hold is set.
    gw = FakeLocalGateway(running=set())
    coord = _coord(
        gw, monkeypatch, total=128.0, used=40.0, hold_loader=lambda: _held("qwen3-coder-next")
    )
    await coord.ensure_room("qwen3-coder-next")  # no raise; fits, so no eviction
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_hold_exempts_the_reserved_planner_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reserved SET is executor + planner (jcode's own plan↔execute), so the planner load
    # (grok's `plan` subagent) is NOT refused even though it's a distinct model — only chat /
    # vision / background work is. Here the coder is resident and the planner loads beside it.
    gw = FakeLocalGateway(running={"qwen3-coder-next"})
    coord = _coord(
        gw,
        monkeypatch,
        total=128.0,
        used=59.6,
        hold_loader=lambda: _held("qwen3-coder-next", "gpt-oss-120b"),
    )
    await coord.ensure_room("gpt-oss-120b")  # reserved planner → no raise (normal evict-to-fit)
    assert "gpt-oss-120b" not in gw.unloaded  # the planner wasn't refused


@pytest.mark.asyncio
async def test_hold_allows_an_already_resident_model_to_keep_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model already resident may keep serving (no new load, no memory pressure) — only a
    # NON-resident, non-reserved model is refused. gpt-oss is resident beside the coder here.
    gw = FakeLocalGateway(running={"qwen3-coder-next", "gpt-oss-120b"})
    coord = _coord(
        gw, monkeypatch, total=128.0, used=100.0, hold_loader=lambda: _held("qwen3-coder-next")
    )
    await coord.ensure_room("gpt-oss-120b")  # already resident → no raise, no evict
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_hold_empty_is_normal_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Code mode off (empty set held) → the loader is inert and normal eviction applies.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0, hold_loader=lambda: _held())
    await coord.ensure_room("qwen3-coder-next")  # 90+59.6 > 96 → normal evict of gpt-oss
    assert gw.unloaded == ["gpt-oss-120b"]


@pytest.mark.asyncio
async def test_hold_skips_the_opportunistic_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    # A restore load bypasses ensure_room's refusal, so while code mode holds the box the
    # opportunistic restore must NOT reload displaced members beside code mode's own — they
    # stay pending until the hold clears (jcode OFF clears it before firing its own restore).
    gw = FakeLocalGateway(running={"qwen3-coder-next"})
    coord = _coord(
        gw, monkeypatch, total=128.0, used=59.6, hold_loader=lambda: _held("qwen3-coder-next")
    )
    coord.note_evicted(["qwen3-vl-30b-a3b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == []  # nothing restored while held
    assert coord._displaced == {"qwen3-vl-30b-a3b"}  # noqa: SLF001  # still pending


# --- the live free-RAM floor override (fraction_loader) ----------------------


@pytest.mark.asyncio
async def test_fraction_loader_override_lowers_the_floor_and_co_resides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (63.5) resident; loading qwen3-vl (33.5) → used 97. At the 0.25 config default
    # the ceiling is 96, so it would evict gpt-oss — but the operator's live 0.15 override
    # (ceiling 108.8) lets the two co-reside. This is exactly the "adjust the floor instead of
    # a lower quant" case the settings card enables.
    async def loader() -> float:
        return 0.15

    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=63.5, fraction_loader=loader)
    await coord.ensure_room("qwen3-vl-30b-a3b")
    assert gw.unloaded == []
    assert coord._displaced == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_without_override_the_config_default_floor_evicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same box with NO override: the 0.25 config default applies (ceiling 96), so
    # gpt-oss is evicted to make room for qwen3-vl — the behavior the override changes.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=63.5)
    await coord.ensure_room("qwen3-vl-30b-a3b")
    assert gw.unloaded == ["gpt-oss-120b"]


@pytest.mark.asyncio
async def test_fraction_loader_invalid_or_failing_falls_back_to_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A junk override (outside (0, 1)) and a loader that raises both degrade to the config
    # default floor — the budget must never lose its floor — so gpt-oss is evicted as in the
    # no-override case.
    async def junk() -> float:
        return 1.5

    async def boom() -> float:
        raise RuntimeError("settings store down")

    for loader in (junk, boom):
        gw = FakeLocalGateway(running={"gpt-oss-120b"})
        coord = _coord(gw, monkeypatch, total=128.0, used=63.5, fraction_loader=loader)
        await coord.ensure_room("qwen3-vl-30b-a3b")
        assert gw.unloaded == ["gpt-oss-120b"], f"loader={loader.__name__}"


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


# --- plan_load: the dry-run eviction preview (no side effects) ---------------


@pytest.mark.asyncio
async def test_plan_load_reports_the_eviction_without_touching_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (63.5) resident, used=90; loading the coder (59.6) would blow the 96 ceiling —
    # the plan says evict gpt-oss, projects the landing point, and unloads NOTHING.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0)
    plan = await coord.plan_load("qwen3-coder-next")
    assert plan is not None
    assert plan.victims == ("gpt-oss-120b",)
    assert plan.fits is False and plan.over is False and plan.already_resident is False
    assert plan.resident_gb == 90.0
    assert round(plan.projected_gb, 1) == round(90.0 - 63.5 + 59.6, 1)  # 86.1
    assert plan.ceiling_gb == 96.0
    assert gw.unloaded == []  # dry-run — nothing evicted


@pytest.mark.asyncio
async def test_a_second_slot_doubles_the_kv_in_the_eviction_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dedicated interactive slot doubles gpt-oss's KV (its footprint jumps from 63.5 to 68.0).
    # Sized so the single-slot load fits under the 96 ceiling but the two-slot load does NOT —
    # proving the doubled KV reaches the eviction budget (else the operator's opt-in would
    # silently overcommit the box).
    async def two_slots() -> dict[str, int]:
        return {"gpt-oss-120b": 2}

    gw = FakeLocalGateway(running={"qwen3.5-4b"})  # a tiny model resident, evictable
    # Single slot (default loader): fits, no eviction.
    fits = await _coord(gw, monkeypatch, total=128.0, used=30.0).plan_load("gpt-oss-120b")
    assert fits is not None and fits.victims == () and fits.fits is True
    # Two slots: the extra 4.5 GB KV tips it over the ceiling → must evict the tiny model.
    over = await _coord(gw, monkeypatch, total=128.0, used=30.0, slots_loader=two_slots).plan_load(
        "gpt-oss-120b"
    )
    assert over is not None and over.victims == ("qwen3.5-4b",)


@pytest.mark.asyncio
async def test_plan_load_fits_with_no_victims(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=40.0)
    plan = await coord.plan_load("qwen3.5-4b")  # 4.6 → 44.6, well under 96
    assert plan is not None
    assert plan.fits is True and plan.victims == ()


@pytest.mark.asyncio
async def test_plan_load_flags_already_resident(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=50.0)
    plan = await coord.plan_load("qwen3.5-4b")
    assert plan is not None
    assert plan.already_resident is True and plan.fits is True and plan.victims == ()


@pytest.mark.asyncio
async def test_plan_load_flags_over_when_it_takes_the_whole_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 64 GB box (ceiling 48): the coder (59.6) exceeds the floor even after evicting gpt-oss —
    # over is True (it takes the box), and it still lists what it would evict.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=64.0, used=63.5)
    plan = await coord.plan_load("qwen3-coder-next")
    assert plan is not None
    assert plan.victims == ("gpt-oss-120b",) and plan.over is True


@pytest.mark.asyncio
async def test_plan_load_is_none_when_disabled_or_unmeasurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    assert await ResidencyCoordinator(gw, enabled=False).plan_load("qwen3.5-4b") is None
    monkeypatch.setattr("jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": None)
    coord = ResidencyCoordinator(gw, models_dir="", enabled=True)
    assert await coord.plan_load("qwen3.5-4b") is None


# --- free_room: the operator's deliberate load (evict, but don't record) -----


@pytest.mark.asyncio
async def test_free_room_evicts_to_fit_but_does_not_record_for_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same eviction as ensure_room, but a manual load is a steady-state change — the evicted
    # model is NOT queued for restore (else the next turn's restore would undo the operator).
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0)
    await coord.free_room("qwen3-coder-next")
    assert gw.unloaded == ["gpt-oss-120b"]
    assert coord._displaced == set()  # noqa: SLF001 — deliberately not recorded


@pytest.mark.asyncio
async def test_free_room_evicts_nothing_when_it_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=40.0)
    await coord.free_room("qwen3.5-4b")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_free_room_is_a_noop_when_disabled() -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3-vl-30b-a3b"})
    await ResidencyCoordinator(gw, enabled=False).free_room("qwen3-coder-next")
    assert gw.unloaded == []


# --- over-box guard: refuse a load that can't physically fit the box -----------


@pytest.mark.asyncio
async def test_plan_load_flags_over_box_when_it_cannot_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 20 GB box can't hold gpt-oss (63.5) no matter what — over_box is set.
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=20.0, used=2.0)
    plan = await coord.plan_load("gpt-oss-120b")
    assert plan is not None
    assert plan.over_box is True and plan.over is True


@pytest.mark.asyncio
async def test_ensure_room_refuses_an_over_box_load_without_evicting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (63.5) can't fit a 20 GB box even after evicting the resident tiny model — refuse
    # with ResidencyError and evict NOTHING (never destroy a resident model for a doomed load).
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    coord = _coord(gw, monkeypatch, total=20.0, used=6.0)
    with pytest.raises(ResidencyError):
        await coord.ensure_room("gpt-oss-120b")
    assert gw.unloaded == []
    assert coord._displaced == set()  # noqa: SLF001 — nothing recorded


@pytest.mark.asyncio
async def test_free_room_refuses_an_over_box_load_without_evicting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    coord = _coord(gw, monkeypatch, total=20.0, used=6.0)
    with pytest.raises(ResidencyError):
        await coord.free_room("gpt-oss-120b")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_takes_the_box_when_it_fits_total_even_if_over_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The "take the box" case is NOT refused: the coder (59.6) is over the 48 floor of a 64 GB
    # box but fits total, so it evicts everything and loads (over, but not over_box).
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=64.0, used=63.5)
    plan = await coord.plan_load("qwen3-coder-next")
    assert plan is not None and plan.over is True and plan.over_box is False
    await coord.ensure_room("qwen3-coder-next")  # no raise — it fits the box
    assert gw.unloaded == ["gpt-oss-120b"]


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
    await coord._restore()  # noqa: SLF001 — empty displaced set
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

    # Nothing displaced → never schedules anything.
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


# --- box lock: cross-process serialization of evict+load ---------------------


class _RecordingGateway(FakeLocalGateway):
    """A gateway that logs each unload/load into a shared list, so a test can assert they
    happened strictly BETWEEN the box lock's acquire and release."""

    def __init__(self, events: list[str], running: set[str] | None = None) -> None:
        super().__init__(running=running)
        self._events = events

    async def unload(self, served_model: str) -> None:
        self._events.append(f"unload:{served_model}")
        await super().unload(served_model)

    async def load(
        self,
        served_model: str,
        *,
        warm_system: str | None = None,
        warm_tools: list[dict[str, object]] | None = None,
    ) -> None:
        self._events.append(f"load:{served_model}")
        await super().load(served_model, warm_system=warm_system, warm_tools=warm_tools)


def _recording_lock(events: list[str]):
    @contextlib.asynccontextmanager
    async def _lock() -> AsyncIterator[None]:
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    return _lock


@pytest.mark.asyncio
async def test_box_lock_serializes_evict_and_load_of_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a box_lock, ensure_room evicts the victim AND loads the target — both strictly
    # inside the lock — so the target's memory is committed before the lock releases and a
    # concurrent process's plan can see it (no cross-process co-load).
    events: list[str] = []
    gw = _RecordingGateway(events, running={"gpt-oss-120b"})
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    coord = ResidencyCoordinator(
        gw, models_dir="", enabled=True, free_ram_fraction=0.25, box_lock=_recording_lock(events)
    )
    await coord.ensure_room("qwen3-coder-next")  # 90+59.6 > 96 → evict gpt-oss, then load it
    assert events == ["lock", "unload:gpt-oss-120b", "load:qwen3-coder-next", "unlock"]


@pytest.mark.asyncio
async def test_box_lock_fast_path_takes_no_lock_when_already_resident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The common case (model already up) must not pay the cross-process lock or reload.
    events: list[str] = []
    gw = _RecordingGateway(events, running={"gpt-oss-120b"})
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    coord = ResidencyCoordinator(
        gw, models_dir="", enabled=True, free_ram_fraction=0.25, box_lock=_recording_lock(events)
    )
    await coord.ensure_room("gpt-oss-120b")  # already resident
    assert events == []  # no lock, no unload, no load
    assert gw.loaded == []


@pytest.mark.asyncio
async def test_box_lock_degrades_to_unlocked_when_acquire_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A lock-infra hiccup (DB down) must not fail the turn: proceed UNLOCKED — the free-RAM
    # budget still evicts and loads; only cross-process serialization is lost for that load.
    @contextlib.asynccontextmanager
    async def failing_lock() -> AsyncIterator[None]:
        raise RuntimeError("db down")
        yield  # pragma: no cover - unreachable, satisfies the generator contract

    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    coord = ResidencyCoordinator(
        gw, models_dir="", enabled=True, free_ram_fraction=0.25, box_lock=failing_lock
    )
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == ["gpt-oss-120b"]  # still evicted, unlocked
    assert gw.loaded == ["qwen3-coder-next"]  # and still loaded


# --- the prefix-lost hook ----------------------------------------------------
# Residency is the only component that knows a model's primed KV just went away. The keeper
# cannot infer it: an evict + restore inside one tick interval leaves `running()` unchanged.


async def test_note_evicted_reports_the_dropped_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    lost: list[str] = []
    coord = _coord(FakeLocalGateway(), monkeypatch, total=128, used=10, on_prefix_lost=lost.append)
    coord.note_evicted(["gpt-oss-120b"])
    assert lost == ["gpt-oss-120b"]


async def test_unknown_served_name_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised name is not tracked for restore, so it has no prime to invalidate."""
    lost: list[str] = []
    coord = _coord(FakeLocalGateway(), monkeypatch, total=128, used=10, on_prefix_lost=lost.append)
    coord.note_evicted(["not-a-catalog-model"])
    assert lost == []


async def test_a_raising_hook_never_breaks_an_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook is a notification. A listener that throws must not turn a successful eviction
    into a failed one — residency's job does not depend on anyone listening."""

    def boom(_served: str) -> None:
        raise RuntimeError("listener exploded")

    coord = _coord(FakeLocalGateway(), monkeypatch, total=128, used=10, on_prefix_lost=boom)
    coord.note_evicted(["gpt-oss-120b"])  # must not raise
    assert "gpt-oss-120b" in coord._displaced


async def test_disabled_box_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    lost: list[str] = []
    coord = _coord(
        FakeLocalGateway(),
        monkeypatch,
        total=128,
        used=10,
        enabled=False,
        on_prefix_lost=lost.append,
    )
    coord.note_evicted(["gpt-oss-120b"])
    assert lost == []
