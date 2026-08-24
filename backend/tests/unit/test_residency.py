"""Residency: the app as sole evictor (ensure_room) and the evict→restore cycle."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from jbrain.llm import local_catalog
from jbrain.llm.admission import Phase, Pool, Reservation
from jbrain.llm.residency import (
    ResidencyCoordinator,
    ResidencyError,
    ResidencyWiring,
    pg_box_try_lock,
)
from tests.unit.fakes import FakeLocalGateway

# Footprints at default windows (weights + KV) used by these tests, from the catalog:
#   gpt-oss-120b        = 59.0 + 5.01*2         = 69.02 (kv_full_history: --swa-full)
#     — the 5.01 was 4.5 until it was MEASURED on the box at four windows and found 11.4%
#       light. Where a case below needs this figure, it asks the catalog rather than
#       repeating it: a literal here is a number that goes stale the next time it is measured,
#       silently, in a file whose whole subject is memory arithmetic.
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
    auto_restore_loader: object = None,
    box_try_lock: object = None,
) -> ResidencyCoordinator:
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (total, used)
    )
    return ResidencyCoordinator(
        gw,
        ResidencyWiring.inert(
            models_dir="",  # nominal catalog size_gb, no filesystem read
            enabled=enabled,
            free_ram_fraction=0.25,
            fraction_loader=fraction_loader,  # type: ignore[arg-type]
            hold_loader=hold_loader,  # type: ignore[arg-type]
            slots_loader=slots_loader,  # type: ignore[arg-type]
            on_prefix_lost=on_prefix_lost,  # type: ignore[arg-type]
            auto_restore_loader=auto_restore_loader,  # type: ignore[arg-type]
            box_try_lock=box_try_lock,  # type: ignore[arg-type]
        ),
    )


def _model(model_id: str) -> local_catalog.LocalModel:
    """A catalog entry, or a hard failure — never a None that turns into a silent zero in an
    arithmetic assertion."""
    model = local_catalog.get(model_id)
    assert model is not None, f"{model_id} left the catalog; this test's premise is gone"
    return model


# --- ensure_room: evict the fewest to fit -----------------------------------


@pytest.mark.asyncio
async def test_ensure_room_evicts_the_big_model_and_keeps_the_tiny_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss (68.0) + the tiny model resident, used=66; loading the coder (59.6) would blow
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
    # gpt-oss (68.0) resident; loading qwen3-vl (33.5) → used 97. At the 0.25 config default
    # the ceiling is 96, so it would evict gpt-oss — but the operator's live 0.15 override
    # (ceiling 108.8) lets the two co-reside. This is exactly the "adjust the floor instead of
    # a lower quant" case the settings card enables.
    async def loader() -> float:
        return 0.15

    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=160.0, used=63.5, fraction_loader=loader)
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
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False))
    await coord.ensure_room("qwen3-coder-next")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ensure_room_best_effort_when_memory_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    monkeypatch.setattr("jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": None)
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(models_dir="", enabled=True))
    await coord.ensure_room("qwen3-coder-next")  # can't measure RAM → evict nothing, no raise
    assert gw.unloaded == []


# --- note_evicted -----------------------------------------------------------


def test_note_evicted_records_only_known_catalog_models() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=True))
    coord.note_evicted(["gpt-oss-120b", "not-a-real-model"])
    assert coord._displaced == {"gpt-oss-120b"}  # noqa: SLF001 — the unknown name is ignored


def test_note_evicted_is_a_noop_when_disabled() -> None:
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False))
    coord.note_evicted(["gpt-oss-120b"])
    assert coord._displaced == set()  # noqa: SLF001


# --- plan_load: the dry-run eviction preview (no side effects) ---------------


@pytest.mark.asyncio
async def test_plan_load_reports_the_eviction_without_touching_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpt-oss resident, used=90; loading the coder (59.6) would blow the 96 ceiling — the plan
    # says evict gpt-oss, projects the landing point, and unloads NOTHING.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0)
    plan = await coord.plan_load("qwen3-coder-next")
    assert plan is not None
    assert plan.victims == ("gpt-oss-120b",)
    assert plan.fits is False and plan.over is False and plan.already_resident is False
    assert plan.resident_gb == 90.0
    # Both terms come from the catalog, not from literals. This assertion is about the
    # projection ARITHMETIC — what leaves, what arrives — so pinning either model's size here
    # would make a re-measurement look like a bug in `plan_load`.
    #
    # It also stops a coincidence from hiding an error, which is what happened here: the
    # literals were 68.0 and 59.6 when the real figures were 68.55 and 60.15. BOTH were 0.55
    # light — each had dropped the runtime-overhead term — and since one is subtracted and the
    # other added, the two mistakes cancelled exactly. The assertion passed for years while
    # neither of its numbers was right, and it only surfaced when a measurement moved one of
    # them and broke the symmetry.
    evicted = local_catalog.footprint_gb(_model("gpt-oss-120b"), 131072)
    arriving = local_catalog.load_footprint_gb(_model("qwen3-coder-next"), 262144)
    assert plan.target_gb == pytest.approx(arriving)
    assert plan.projected_gb == pytest.approx(90.0 - evicted + arriving)
    assert plan.ceiling_gb == 96.0
    assert gw.unloaded == []  # dry-run — nothing evicted


@pytest.mark.asyncio
async def test_a_second_slot_doubles_the_kv_in_the_eviction_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dedicated interactive slot doubles gpt-oss's KV again: 68.55 -> 77.55 resident (59
    # weights + two slots of full-history KV + the 0.55 runtime term). No prompt-cache term
    # since the gateway serves `-cram 0`.
    # Sized so the single-slot load fits under the ceiling but the two-slot load does NOT —
    # proving the doubled KV reaches the eviction budget (else the operator's opt-in would
    # silently overcommit the box). used=25 on a 128 GB box, ceiling 96:
    # 25+68.55=93.6 fits, 25+77.55=102.6 does not.
    async def two_slots() -> dict[str, int]:
        return {"gpt-oss-120b": 2}

    gw = FakeLocalGateway(running={"qwen3.5-4b"})  # a tiny model resident, evictable
    # Single slot (default loader): fits, no eviction.
    fits = await _coord(gw, monkeypatch, total=128.0, used=25.0).plan_load("gpt-oss-120b")
    assert fits is not None and fits.victims == () and fits.fits is True
    # Two slots: the extra 9.0 GB KV tips it over the ceiling → must evict the tiny model.
    over = await _coord(gw, monkeypatch, total=128.0, used=25.0, slots_loader=two_slots).plan_load(
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
    assert (
        await ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False)).plan_load("qwen3.5-4b")
        is None
    )
    monkeypatch.setattr("jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": None)
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(models_dir="", enabled=True))
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
    await ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False)).free_room(
        "qwen3-coder-next"
    )
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
    # loads them both back and clears the displaced set. The box is sized so both footprints
    # fit under the ceiling — vl now carries its CLIP attention buffer, so this needs more
    # room than it did when a vision model was costed as weights + KV alone.
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=200.0, used=10.0)
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
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=True))
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
    coord = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False))
    coord.note_evicted(["gpt-oss-120b"])  # no-op (disabled), so nothing to restore
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == []


# --- schedule_restore -------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_restore_warms_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=200.0, used=10.0)  # ceiling 150 — both members fit
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
    empty = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=True))
    empty.schedule_restore()
    assert empty._tasks == set()  # noqa: SLF001

    # Disabled box never schedules either.
    disabled = ResidencyCoordinator(gw, ResidencyWiring.inert(enabled=False))
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
        warm_reasoning_effort: str | None = None,
        before_warm=None,
        after_warm=None,
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
async def test_box_lock_covers_the_evict_and_releases_before_the_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a box_lock, ensure_room decides and evicts INSIDE the lock and loads the target
    # strictly AFTER it releases. The load must never run under this key: `ledger.charge`
    # takes the same advisory lock on its own pooled connection, so a load inside the lock
    # refuses its own admission — the 2026-08-23 self-deadlock, which evicted the resident
    # model and then left the box empty. The charge row, not the lock, is what a concurrent
    # process's plan sees during the load.
    events: list[str] = []
    gw = _RecordingGateway(events, running={"gpt-oss-120b"})
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    coord = ResidencyCoordinator(
        gw,
        ResidencyWiring.inert(
            models_dir="", enabled=True, free_ram_fraction=0.25, box_lock=_recording_lock(events)
        ),
    )
    await coord.ensure_room("qwen3-coder-next")  # 90+59.6 > 96 → evict gpt-oss, then load it
    assert events == ["lock", "unload:gpt-oss-120b", "unlock", "load:qwen3-coder-next"]


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
        gw,
        ResidencyWiring.inert(
            models_dir="", enabled=True, free_ram_fraction=0.25, box_lock=_recording_lock(events)
        ),
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
        gw,
        ResidencyWiring.inert(
            models_dir="", enabled=True, free_ram_fraction=0.25, box_lock=failing_lock
        ),
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


@pytest.mark.asyncio
async def test_the_restore_is_skipped_when_the_operator_turned_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's "nothing loads unless I ask" switch. A restore is a model load they did
    not ask for at that moment; with the switch off the displaced set stays displaced and
    nothing reaches the gateway — on a box with room to spare, so only the switch explains
    the skip."""

    async def _off() -> bool:
        return False

    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=160.0, used=10.0, auto_restore_loader=_off)
    coord.note_evicted(["gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == []
    assert coord._displaced == {"gpt-oss-120b"}  # noqa: SLF001 — still pending, not dropped


@pytest.mark.asyncio
async def test_the_restore_defaults_on_when_the_switch_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings-store hiccup must not silently leave the box refusing to drift back to its
    steady state — the failure mode of a knob nobody knows is stuck."""

    async def _explodes() -> bool:
        raise RuntimeError("settings store down")

    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=160.0, used=10.0, auto_restore_loader=_explodes)
    coord.note_evicted(["gpt-oss-120b"])
    await coord._restore()  # noqa: SLF001
    assert gw.loaded == ["gpt-oss-120b"]


async def test_a_device_refusal_from_the_gateway_reaches_the_caller() -> None:
    """A refused load must surface, not be swallowed with the housekeeping errors.

    The device guard itself no longer lives here — it moved into `LocalGatewayClient.load`,
    the one chokepoint every caller passes through, because this coordinator was only one of
    six and the three freezes all came in through the other five. What this layer still owes
    is honesty: `ensure_room` suppresses `LocalGatewayError` (a gateway hiccup is
    best-effort), and a `GpuBudgetError` must NOT ride along on that suppression. The
    operator has to learn the box declined, rather than silently get no model."""
    from jbrain.llm import gpu_guard

    class _RefusingGateway(FakeLocalGateway):
        async def load(self, served_model: str, **kw: object) -> None:
            raise gpu_guard.GpuBudgetError("no device room")

    gw = _RefusingGateway(running=set())
    coord = ResidencyCoordinator(
        gw, ResidencyWiring.inert(enabled=True, free_ram_fraction=0.05, box_lock=None)
    )
    with pytest.raises(gpu_guard.GpuBudgetError):
        await coord._guarded_load("qwen3.8-27b-q4", projected_gb=21.0)


async def test_the_load_measurement_is_logged_against_the_prediction() -> None:
    """What the coordinator keeps after the guard moved out: the post-load measurement. The
    delta between predicted and measured is how the catalog numbers get corrected from data
    instead of from the next freeze."""
    from jbrain.llm import gpu_guard

    class _ClimbingProbe:
        def __init__(self) -> None:
            self._used = 2.0

        async def sample(self) -> gpu_guard.GpuMem | None:
            sample = gpu_guard.GpuMem(
                gtt_used_gb=self._used, gtt_total_gb=120.0, vram_used_gb=0.0, vram_total_gb=0.0
            )
            self._used += 19.0
            return sample

    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(
        gw, ResidencyWiring.inert(enabled=True, gpu_probe=_ClimbingProbe(), box_lock=None)
    )
    await coord._guarded_load("qwen3.8-27b-q4", projected_gb=21.0)
    assert gw.loaded == ["qwen3.8-27b-q4"] and gw.unloaded == []


async def test_a_failed_load_is_not_reported_as_a_measured_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A gateway failure stays non-fatal but must not be logged as a successful load.

    `load_measured` on a load that never happened reads as a model that came in near zero —
    exactly backwards from the truth, and it poisons the predicted-vs-measured series the
    catalog numbers are corrected from. The failure gets its own event instead."""
    from jbrain.llm import gpu_guard
    from jbrain.llm.local_gateway import LocalGatewayError

    class _FailingGateway(FakeLocalGateway):
        async def load(self, served_model: str, **kw: object) -> None:
            raise LocalGatewayError("gateway unreachable")

    class _SteadyProbe:
        async def sample(self) -> gpu_guard.GpuMem | None:
            return gpu_guard.GpuMem(
                gtt_used_gb=10.0, gtt_total_gb=120.0, vram_used_gb=0.0, vram_total_gb=0.0
            )

    gw = _FailingGateway(running=set())
    coord = ResidencyCoordinator(
        gw, ResidencyWiring.inert(enabled=True, gpu_probe=_SteadyProbe(), box_lock=None)
    )
    await coord._guarded_load("qwen3.8-27b-q4", projected_gb=21.0)  # non-fatal, as before
    text = capsys.readouterr().out
    assert "residency.load_failed" in text
    assert "load_measured" not in text


async def test_a_healthy_load_still_goes_through_untouched() -> None:
    """The guard must be invisible in the normal case. A guard that trips on healthy loads
    gets switched off, and then it protects nothing."""
    from jbrain.llm import gpu_guard

    class _SteadyProbe:
        async def sample(self) -> gpu_guard.GpuMem | None:
            return gpu_guard.GpuMem(
                gtt_used_gb=10.0, gtt_total_gb=120.0, vram_used_gb=0.0, vram_total_gb=2.0
            )

    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(
        gw, ResidencyWiring.inert(enabled=True, gpu_probe=_SteadyProbe(), box_lock=None)
    )
    await coord._guarded_load("qwen3.8-27b-q4", projected_gb=21.0)
    assert gw.loaded == ["qwen3.8-27b-q4"] and gw.unloaded == []


async def test_without_a_probe_the_load_path_is_unchanged() -> None:
    """A box that can't measure device memory (no amdgpu, supervisor down, every existing
    test) must keep working exactly as before — degrade, never block."""
    gw = FakeLocalGateway(running=set())
    coord = ResidencyCoordinator(
        gw, ResidencyWiring.inert(enabled=True, gpu_probe=None, box_lock=None)
    )
    await coord._guarded_load("qwen3.8-27b-q4", projected_gb=21.0)
    assert gw.loaded == ["qwen3.8-27b-q4"]


# --- the already-resident short-circuit, and WHY it fired ---------------------------
# `/running` lists a model llama-swap is STOPPING, so "resident" here can mean "on its
# way out". This wave narrates that case; it does not yet change what admission does.


def _warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """structlog does not route through caplog; capture the event names directly."""
    seen: list[str] = []
    monkeypatch.setattr("jbrain.llm.residency.log.warning", lambda ev, **kw: seen.append(ev))
    return seen


async def test_short_circuit_narrates_a_not_ready_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _warnings(monkeypatch)
    gw = FakeLocalGateway({"gpt-oss-120b"})
    gw.states = {"gpt-oss-120b": "stopping"}
    coord = _coord(gw, monkeypatch, total=121.0, used=10.0)
    await coord.ensure_room("gpt-oss-120b")
    assert "residency.short_circuit_not_ready" in seen
    # Still a no-op on the box: the behaviour change is the NEXT wave.
    assert gw.unloaded == []
    assert gw.loaded == []


async def test_short_circuit_is_silent_for_a_ready_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _warnings(monkeypatch)
    gw = FakeLocalGateway({"gpt-oss-120b"})
    gw.states = {"gpt-oss-120b": "ready"}
    coord = _coord(gw, monkeypatch, total=121.0, used=10.0)
    await coord.ensure_room("gpt-oss-120b")
    assert seen == []


async def test_short_circuit_is_silent_when_the_build_reports_no_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown state is not evidence of a problem — it must not cry wolf on a
    gateway build that reports no state at all."""
    seen = _warnings(monkeypatch)
    gw = FakeLocalGateway({"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=121.0, used=10.0)
    await coord.ensure_room("gpt-oss-120b")
    assert seen == []


class _StoppingGateway(FakeLocalGateway):
    """Reports its model as `stopping` — llama-swap lists a model it is shutting down in
    `/running`, which is the read the short-circuit narration exists to catch."""

    def state_of(self, served_model: str) -> str:
        return "stopping"


@pytest.mark.asyncio
async def test_the_stage_preview_does_not_count_as_a_skipped_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`short_circuit_not_ready` is the MEASUREMENT the running()/ready() split is gated on, so
    what increments it has to be a real skipped admission and nothing else.

    `plan_load` is the settings screen's stage preview — it admits nothing and its docstring
    says "No side effects" — but it shares `_plan` with `ensure_room`/`free_room`. Narrating
    from inside `_plan` counted every preview the operator opened, which would have read back
    as evidence of the very bug the counter exists to confirm."""
    gw = _StoppingGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=40.0, enabled=True)
    seen: list[str] = []
    monkeypatch.setattr("jbrain.llm.residency.log.warning", lambda ev, **kw: seen.append(ev))

    await coord.plan_load("gpt-oss-120b")  # the preview: resident+stopping, short circuit hit
    assert "residency.short_circuit_not_ready" not in seen, (
        "the stage preview inflated the counter the next wave reads"
    )

    await coord.ensure_room("gpt-oss-120b")  # a REAL admission skipping on the same read
    assert "residency.short_circuit_not_ready" in seen, "a real skipped admission went unnarrated"


@pytest.mark.asyncio
async def test_an_operator_load_evicts_code_modes_model_but_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-decided 2026-08-22: the Load button outranks code mode's reservation, so this
    does NOT refuse the way `ensure_room` refuses a chat turn. The defect was the silence —
    the coder disappeared mid-session behind a reason that read like routine housekeeping, and
    `free_room` schedules no restore, so it stayed gone.

    Both halves are pinned here: the eviction still happens (authority preserved), and it is
    narrated as the held model it was (traceability restored)."""
    gw = FakeLocalGateway(running={"qwen3-coder-next"})
    coord = _coord(
        gw,
        monkeypatch,
        total=128.0,
        used=90.0,
        enabled=True,
        hold_loader=lambda: _held("qwen3-coder-next"),
    )
    warnings: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jbrain.llm.residency.log.warning",
        lambda ev, **kw: warnings.append({"event": ev, **kw}),
    )

    await coord.free_room("gpt-oss-120b")

    assert gw.unloaded == ["qwen3-coder-next"], "the operator's load must still win"
    held_warnings = [w for w in warnings if w["event"] == "residency.evicted_held_model"]
    assert held_warnings, "code mode lost its model with no warning — the silent break"
    assert held_warnings[0]["model"] == "qwen3-coder-next"
    assert held_warnings[0]["for_model"] == "gpt-oss-120b"


@pytest.mark.asyncio
async def test_a_failed_unload_does_not_claim_code_mode_lost_its_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unload's `LocalGatewayError` is suppressed, so warning BEFORE it asserted a loss
    that may not have happened — sending the owner to debug a code session that is fine while
    the model is still resident."""

    from jbrain.llm.local_gateway import LocalGatewayError

    class _RefusingGateway(FakeLocalGateway):
        async def unload(self, served_model: str) -> None:
            raise LocalGatewayError("gateway refused")

    gw = _RefusingGateway(running={"qwen3-coder-next"})
    coord = _coord(
        gw,
        monkeypatch,
        total=128.0,
        used=90.0,
        enabled=True,
        hold_loader=lambda: _held("qwen3-coder-next"),
    )
    warnings: list[str] = []
    monkeypatch.setattr("jbrain.llm.residency.log.warning", lambda ev, **kw: warnings.append(ev))

    await coord.free_room("gpt-oss-120b")

    assert "residency.evicted_held_model" not in warnings, (
        "claimed code mode lost its model, but the unload failed and it is still resident"
    )


@pytest.mark.asyncio
async def test_an_operator_load_clears_a_victim_that_was_awaiting_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`free_room` records no restore for what it evicts — but a victim already displaced by an
    EARLIER transient displacement was still sitting in `_displaced`, so the next
    `schedule_restore` reloaded it. The restore fighting the operator is exactly what this path
    exists to avoid, and it made the new vitals reason ("will NOT be restored") false."""
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    coord = _coord(gw, monkeypatch, total=128.0, used=90.0, enabled=True)
    coord._displaced.add("gpt-oss-120b")  # pending restore from an earlier image render

    await coord.free_room("qwen3-coder-next")

    assert gw.unloaded == ["gpt-oss-120b"]
    assert "gpt-oss-120b" not in coord._displaced, (
        "the operator's victim is still queued for restore — it will come back behind them"
    )


@pytest.mark.asyncio
async def test_the_code_mode_short_circuit_is_measured_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three places conclude "already resident" from `/running`, not two: the fast path, the
    plan, and the code-mode hold branch. The hold branch went unmeasured, so the counter the
    running()/ready() split is gated on under-reported precisely while code mode owned the box
    — a state the owner is in for hours at a time."""
    gw = _StoppingGateway(running={"gpt-oss-120b"})
    coord = _coord(
        gw,
        monkeypatch,
        total=128.0,
        used=40.0,
        enabled=True,
        hold_loader=lambda: _held("qwen3-coder-next"),
    )
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        "jbrain.llm.residency.log.warning", lambda ev, **kw: seen.append({"event": ev, **kw})
    )

    await coord.ensure_room("gpt-oss-120b")  # resident+stopping, and code mode holds the box

    hits = [e for e in seen if e["event"] == "residency.short_circuit_not_ready"]
    assert hits, "the code-mode short circuit skipped an admission without measuring it"
    assert hits[0]["at"] == "hold"


@pytest.mark.asyncio
async def test_the_over_box_refusal_does_not_blame_the_model_for_the_whole_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`projected_gb` is `used + target - freed` — the whole box after the load. Rendering it
    as "{target} needs ~N GB" told the owner a 17 GB model wanted more memory than the machine
    has. That text reaches the 409 body and the chat turn, so it is the sentence they act on."""
    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=121.0, used=110.0, enabled=True)

    with pytest.raises(ResidencyError) as exc:
        await coord.ensure_room("gpt-oss-120b")

    msg = str(exc.value)
    assert "121 GB" in msg, "the box's real size is missing"
    assert "110 GB" in msg, "what is already resident is missing — the actionable part"
    # the target's own footprint, not the post-load total, must be what it is said to need
    assert "needs ~110 GB" not in msg and "needs ~178 GB" not in msg, (
        f"the model is being blamed for the whole box: {msg}"
    )


@pytest.mark.asyncio
async def test_a_busy_box_skips_the_restore_and_keeps_the_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore takes a TRY-lock, and a refusal means skip — not wait.

    Blocking would be wrong twice over: the steady state being restored to is already being
    changed by whoever holds the lock, and while `_restore` waited it would hold the
    per-process load lock against a chat turn for the length of someone else's 200 s load.

    The load-bearing half of this test is the second assertion. A skipped restore MUST leave
    `_displaced` intact — the normal path discards a member once attempted, and a skip that
    reused that logic would forget the models permanently."""

    @contextlib.asynccontextmanager
    async def _busy() -> AsyncIterator[bool]:
        yield False

    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=40.0, enabled=True, box_try_lock=_busy)
    coord._displaced.update({"gpt-oss-120b", "qwen3.5-4b"})

    await coord._restore()

    assert not gw.loaded, "restored while another process was changing the box"
    assert coord._displaced == {"gpt-oss-120b", "qwen3.5-4b"}, (
        "a skipped restore dropped its members — they would never be restored"
    )


@pytest.mark.asyncio
async def test_an_idle_box_restores_under_the_try_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: when the lock IS free, restore proceeds exactly as before."""

    @contextlib.asynccontextmanager
    async def _free() -> AsyncIterator[bool]:
        yield True

    gw = FakeLocalGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0, enabled=True, box_try_lock=_free)
    coord._displaced.add("qwen3.5-4b")

    await coord._restore()

    assert gw.loaded == ["qwen3.5-4b"]
    assert not coord._displaced


@pytest.mark.asyncio
async def test_the_restore_releases_the_box_lock_BEFORE_it_loads_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The try-lock covers the CENSUS, never the loads. This is the assertion that keeps the
    skip from becoming worse than no lock at all.

    `ensure_room` takes the BLOCKING form of the same advisory lock, and there is no
    `lock_timeout` anywhere in this repo. So a restore holding it across its loads — 100-200 s
    each, several of them — makes a chat turn in the OTHER process wait out a background task,
    with every waiter pinning one of the fifteen pooled connections while it waits. That is not
    an absent convoy, it is the convoy inverted onto the interactive path."""
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def _recording() -> AsyncIterator[bool]:
        events.append("lock")
        try:
            yield True
        finally:
            events.append("unlock")

    class _NotingGateway(FakeLocalGateway):
        async def load(
            self,
            served_model: str,
            *,
            warm_system: str | None = None,
            warm_tools: list[dict[str, object]] | None = None,
            warm_reasoning_effort: str | None = None,
            before_warm=None,
            after_warm=None,
        ) -> None:
            events.append(f"load:{served_model}")
            await super().load(served_model, warm_system=warm_system, warm_tools=warm_tools)

    gw = _NotingGateway(running=set())
    coord = _coord(gw, monkeypatch, total=128.0, used=10.0, enabled=True, box_try_lock=_recording)
    coord._displaced.add("qwen3.5-4b")

    await coord._restore()

    assert gw.loaded == ["qwen3.5-4b"]
    assert events == ["lock", "unlock", "load:qwen3.5-4b"], (
        f"the lock was still held while the model loaded: {events}"
    )


@pytest.mark.asyncio
async def test_a_skipped_restore_arms_a_retry_rather_than_waiting_for_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`schedule_restore` fires from a finished agent turn and from code-mode power-off, and
    nowhere else. So a skip on the last turn of the evening would leave the box displaced until
    somebody starts another conversation — the members are still remembered, all that is
    missing is an occasion. The skip creates one."""
    monkeypatch.setattr("jbrain.llm.residency.RESTORE_RETRY_S", 0.01)

    busy = True

    @contextlib.asynccontextmanager
    async def _busy_then_free() -> AsyncIterator[bool]:
        yield not busy

    gw = FakeLocalGateway(running=set())
    coord = _coord(
        gw, monkeypatch, total=128.0, used=10.0, enabled=True, box_try_lock=_busy_then_free
    )
    coord._displaced.add("qwen3.5-4b")

    await coord._restore()
    assert not gw.loaded, "it restored while the box was busy"

    busy = False
    await asyncio.sleep(0.05)  # let the armed retry fire
    assert gw.loaded == ["qwen3.5-4b"], "the skip never came back for the displaced member"


# --- pg_box_try_lock: the helper itself -------------------------------------


class _FakeSession:
    """Just enough of AsyncSession for the advisory-lock helpers: `begin()` and `scalar()`."""

    def __init__(self, answer: bool) -> None:
        self._answer = answer

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield

    async def scalar(self, _stmt: object, _params: object = None) -> bool:
        return self._answer


def _maker(answer: bool) -> object:
    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession(answer)

    return _session


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [True, False])
async def test_pg_box_try_lock_reports_what_postgres_said(answer: bool) -> None:
    async with pg_box_try_lock(_maker(answer))() as got:  # type: ignore[arg-type]
        assert got is answer


@pytest.mark.asyncio
async def test_pg_box_try_lock_yields_false_when_the_db_is_unreachable() -> None:
    """An opportunistic caller must SKIP, not fail, when the lock can't be consulted. False is
    the safe direction here (unlike `pg_box_lock`, where a DB outage must not fail a turn)."""

    @contextlib.asynccontextmanager
    async def _dead() -> AsyncIterator[_FakeSession]:
        raise RuntimeError("no route to host")
        yield  # pragma: no cover — unreachable, satisfies the generator protocol

    async with pg_box_try_lock(_dead)() as got:  # type: ignore[arg-type]
        assert got is False


@pytest.mark.asyncio
async def test_pg_box_try_lock_does_not_swallow_the_callers_own_failure() -> None:
    """The caller's exception travels back in through the `yield`. Catching it there would
    hide the real error behind `generator didn't stop after throw()` and then hand the caller
    a second, bogus `False` — so the lock must only treat PRE-acquisition failures as skips."""
    with pytest.raises(ValueError, match="the restore itself blew up"):
        async with pg_box_try_lock(_maker(True))() as got:  # type: ignore[arg-type]
            assert got is True
            raise ValueError("the restore itself blew up")


# --- the ledger-arithmetic plan (L3): one calculation for evict and admit ----------------


class _FakeLedger:
    """`pools()` + `live()` as `_plan_ledger` reads them. Raising flavors prove fallback."""

    def __init__(
        self, rows: list[Reservation], host: Pool, device: Pool, *, broken: bool = False
    ) -> None:
        self._rows = rows
        self._host = host
        self._device = device
        self._broken = broken

    async def pools(self) -> tuple[Pool, Pool]:
        if self._broken:
            raise RuntimeError("db down")
        return (self._host, self._device)

    async def live(self) -> list[Reservation]:
        return list(self._rows)


def _row(served: str, phase: Phase, gb: float) -> Reservation:
    return Reservation(
        instance_id=f"{served}-row", served_model=served, phase=phase, host_gb=gb, device_gb=gb
    )


def _ledger_coord(
    gw: FakeLocalGateway,
    ledger: _FakeLedger,
    *,
    free_ram_fraction: float = 0.15,
) -> ResidencyCoordinator:
    return ResidencyCoordinator(
        gw,
        ResidencyWiring.inert(
            models_dir="",
            enabled=True,
            free_ram_fraction=free_ram_fraction,
            ledger=ledger,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_ledger_plan_counts_a_concurrent_processes_in_flight_load() -> None:
    # The row-based plan is what makes releasing the box lock before the load safe: another
    # process's charge is a STARTING row long before its memory shows up in a measurement.
    # Here the worker is mid-load of the coder (59.6 declared, nothing measurable yet) and
    # this process plans gpt-oss: the measured planner would say "10 used + 69 fits", the
    # ledger term says 103.0 usable − 59.6 promised < 69 — and with nothing resident to
    # evict, the plan must come back over (charge would refuse), never fits.
    coder = local_catalog.get_by_served("qwen3-coder-next")
    assert coder is not None
    coder_gb, _ = local_catalog.declared_gb(coder, coder.context_window)
    gw = FakeLocalGateway(running=set())
    ledger = _FakeLedger(
        [_row("qwen3-coder-next", Phase.STARTING, coder_gb)],
        host=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=111.0),
        device=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=111.0),
    )
    plan = await _ledger_coord(gw, ledger).plan_load("gpt-oss-120b")
    assert plan is not None
    assert not plan.fits
    assert plan.victims == ()
    assert plan.over and not plan.over_box


@pytest.mark.asyncio
async def test_ledger_plan_evicts_by_charged_size_until_the_charge_would_admit() -> None:
    # gpt-oss holds a charged 69.6 GB row; the coder needs 59.6 and the ledger term has only
    # ~45 left — one eviction, credited back to BOTH terms at its charged size, and the same
    # `admit` that will run inside the load says yes. Victims come from what the ledger
    # holds, not from a fresh prediction.
    gpt = local_catalog.get_by_served("gpt-oss-120b")
    assert gpt is not None
    gpt_gb, _ = local_catalog.declared_gb(gpt, gpt.context_window)
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    ledger = _FakeLedger(
        [_row("gpt-oss-120b", Phase.RESIDENT, gpt_gb)],
        host=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=43.0),
        device=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=43.0),
    )
    plan = await _ledger_coord(gw, ledger, free_ram_fraction=0.05).plan_load("qwen3-coder-next")
    assert plan is not None
    assert plan.victims == ("gpt-oss-120b",)
    assert not plan.fits and not plan.over and not plan.over_box


@pytest.mark.asyncio
async def test_ledger_plan_admits_a_co_resident_pair_with_no_eviction() -> None:
    # The 2026-08-23 shape with the interactive slot OFF: gpt-oss charged at ~69.6, real
    # free memory 43 GB, and the vision model (33.5 declared) wants in. Both of admission's
    # terms have room, so the plan must load it BESIDE gpt-oss — no victims — where a
    # headroom-vs-overhead disagreement in a second budget could have evicted.
    gpt = local_catalog.get_by_served("gpt-oss-120b")
    assert gpt is not None
    gpt_gb, _ = local_catalog.declared_gb(gpt, gpt.context_window)
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    ledger = _FakeLedger(
        [_row("gpt-oss-120b", Phase.RESIDENT, gpt_gb)],
        host=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=43.0),
        device=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=43.0),
    )
    plan = await _ledger_coord(gw, ledger, free_ram_fraction=0.05).plan_load("qwen3-vl-30b-a3b")
    assert plan is not None
    assert plan.fits and plan.victims == ()


@pytest.mark.asyncio
async def test_ledger_plan_falls_back_to_measured_when_the_ledger_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A DB hiccup must not take eviction planning down with it — the measured planner is
    # still wired underneath, and its verdict (evict gpt-oss for the coder at these numbers)
    # comes through.
    gw = FakeLocalGateway(running={"gpt-oss-120b"})
    monkeypatch.setattr(
        "jbrain.llm.residency.read_memory_gb", lambda path="/proc/meminfo": (128.0, 90.0)
    )
    broken = _FakeLedger([], host=Pool(1.0, 0.0, None), device=Pool(1.0, 0.0, None), broken=True)
    coord = ResidencyCoordinator(
        gw,
        ResidencyWiring.inert(
            models_dir="",
            enabled=True,
            free_ram_fraction=0.25,
            ledger=broken,  # type: ignore[arg-type]
        ),
    )
    plan = await coord.plan_load("qwen3-coder-next")
    assert plan is not None
    assert plan.victims == ("gpt-oss-120b",)


@pytest.mark.asyncio
async def test_ledger_plan_infeasible_refuses_before_evicting_anything() -> None:
    # A model bigger than the box's whole usable pool is INFEASIBLE in admission's terms —
    # over_box here — and ensure_room must refuse (ResidencyError) without unloading a thing.
    gw = FakeLocalGateway(running={"qwen3.5-4b"})
    ledger = _FakeLedger(
        [_row("qwen3.5-4b", Phase.RESIDENT, 4.6)],
        host=Pool(total_gb=60.0, reserve_gb=6.0, measured_free_gb=50.0),
        device=Pool(total_gb=60.0, reserve_gb=6.0, measured_free_gb=50.0),
    )
    coord = _ledger_coord(gw, ledger)
    with pytest.raises(ResidencyError):
        await coord.ensure_room("gpt-oss-120b")
    assert gw.unloaded == []


@pytest.mark.asyncio
async def test_ledger_plan_evicts_the_biggest_first_and_spares_the_tiny_model() -> None:
    # Two charged residents; the coder needs the big one's room and nothing more. The
    # ranking must be biggest-first — a smallest-first order would burn the tiny model's
    # eviction for ~nothing and still have to take the big one (the mutant an adversarial
    # review proved the single-resident tests could not kill, 2026-08-23).
    gpt = local_catalog.get_by_served("gpt-oss-120b")
    assert gpt is not None
    gpt_gb, _ = local_catalog.declared_gb(gpt, gpt.context_window)
    gw = FakeLocalGateway(running={"gpt-oss-120b", "qwen3.5-4b"})
    ledger = _FakeLedger(
        [_row("gpt-oss-120b", Phase.RESIDENT, gpt_gb), _row("qwen3.5-4b", Phase.RESIDENT, 4.6)],
        host=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=43.0),
        device=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=43.0),
    )
    plan = await _ledger_coord(gw, ledger, free_ram_fraction=0.05).plan_load("qwen3-coder-next")
    assert plan is not None
    assert plan.victims == ("gpt-oss-120b",), "one big eviction, the tiny model spared"


@pytest.mark.asyncio
async def test_a_generous_floor_is_never_reported_as_would_crash_the_box() -> None:
    # over_box means PHYSICAL infeasibility — the settings screen renders it permanent
    # ("would crash the box") and ensure_room refuses outright. An operator floor of 45%
    # leaves gpt-oss (~69.6 declared) over the FLOORED usable but comfortably inside the
    # box; that must read as `over` (evict everything, let the charge decide), never
    # over_box — the charge itself would admit it.
    gw = FakeLocalGateway(running=set())
    ledger = _FakeLedger(
        [],
        host=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=110.0),
        device=Pool(total_gb=121.2, reserve_gb=6.0, measured_free_gb=110.0),
    )
    plan = await _ledger_coord(gw, ledger, free_ram_fraction=0.45).plan_load("gpt-oss-120b")
    assert plan is not None
    assert not plan.over_box, "a floor refusal is transient, not 'cannot exist'"
    assert plan.over
