from dataclasses import asdict

import pytest

from jbrain.llm import local_catalog, router, slot_roles
from jbrain.llm.errors import LlmContextOverflowError
from jbrain.llm.slot_roles import (
    FLASH_NEXT_POOL,
    KvPool,
    RoleReservation,
    SlotCapError,
    SlotRole,
    admit,
    pool_shape,
    role_for,
)


def test_flash_next_pool_shape() -> None:
    assert FLASH_NEXT_POOL.n_ctx == 524_288
    assert FLASH_NEXT_POOL.n_slots == 8
    assert {r.role for r in FLASH_NEXT_POOL.reservations} == set(SlotRole)


def test_no_cap_exceeds_the_trained_context() -> None:
    assert max(r.cap_tokens for r in FLASH_NEXT_POOL.reservations) == 262_144


def test_interactive_prefix_is_freed_last_and_small_first() -> None:
    order = FLASH_NEXT_POOL.eviction_order()
    assert order[-1] == FLASH_NEXT_POOL.slot(SlotRole.INTERACTIVE)
    assert order[0] == FLASH_NEXT_POOL.slot(SlotRole.SMALL)


def test_every_routed_task_has_a_role() -> None:
    assert set(router.TASK_DEFAULTS) <= set(slot_roles.TASK_ROLES)


def test_role_for_override_wins_and_unknown_falls_back() -> None:
    assert role_for("agent.turn") is SlotRole.INTERACTIVE
    assert role_for("agent.turn", SlotRole.RESEARCH) is SlotRole.RESEARCH
    assert role_for("debug.whatever") is slot_roles.UNKNOWN_TASK_ROLE


def test_pet_overflows_to_small() -> None:
    assert FLASH_NEXT_POOL.reservation(SlotRole.PET).overflow is SlotRole.SMALL


def test_research_overflows_to_workshop() -> None:
    assert FLASH_NEXT_POOL.reservation(SlotRole.RESEARCH).overflow is SlotRole.WORKSHOP


def _pool(*caps: int, n_ctx: int = 65_536) -> KvPool:
    return KvPool(
        n_ctx,
        tuple(
            RoleReservation(role, i, cap, i, role.value)
            for i, (role, cap) in enumerate(zip(SlotRole, caps, strict=False))
        ),
    )


def test_pool_rejects_gapped_slots() -> None:
    with pytest.raises(ValueError, match="0..n-1"):
        KvPool(
            65_536,
            (RoleReservation(SlotRole.INTERACTIVE, 1, 1024, 0, "x"),),
        )


def test_pool_rejects_cap_over_trained_context() -> None:
    with pytest.raises(ValueError, match="trained context"):
        _pool(300_000, n_ctx=1_048_576)


def test_pool_rejects_duplicate_roles() -> None:
    with pytest.raises(ValueError, match="exactly one slot"):
        KvPool(
            65_536,
            (
                RoleReservation(SlotRole.PET, 0, 1024, 0, "a"),
                RoleReservation(SlotRole.PET, 1, 1024, 1, "b"),
            ),
        )


def test_pool_rejects_overflow_to_missing_role() -> None:
    with pytest.raises(ValueError, match="overflows"):
        KvPool(65_536, (RoleReservation(SlotRole.PET, 0, 1024, 0, "a", SlotRole.SMALL),))


def test_admit_fits_unchanged() -> None:
    got = admit(FLASH_NEXT_POOL, SlotRole.PET, prompt_tokens=2000, max_tokens=1000)
    assert (got.slot, got.max_tokens, got.clamped) == (6, 1000, False)


def test_admit_clamps_output_when_enough_survives() -> None:
    got = admit(FLASH_NEXT_POOL, SlotRole.PET, prompt_tokens=30_000, max_tokens=8000)
    assert got.clamped
    assert got.max_tokens == 32_768 - 30_000


def test_admit_refuses_when_clamp_too_small() -> None:
    with pytest.raises(SlotCapError) as caught:
        admit(FLASH_NEXT_POOL, SlotRole.PET, prompt_tokens=32_000, max_tokens=8000)
    err = caught.value
    assert isinstance(err, LlmContextOverflowError)
    assert (err.role, err.cap, err.prompt_tokens) == (SlotRole.PET, 32_768, 32_000)


def test_admit_refuses_prompt_alone_over_cap() -> None:
    with pytest.raises(SlotCapError):
        admit(FLASH_NEXT_POOL, SlotRole.SMALL, prompt_tokens=70_000, max_tokens=10)


def test_estimate_charges_images_flat() -> None:
    base = slot_roles.estimate_prompt_tokens("unknown-model", chars=3700)
    with_two = slot_roles.estimate_prompt_tokens("unknown-model", chars=3700, n_images=2)
    assert with_two - base == 2 * slot_roles.IMAGE_TOKENS_CHARGE


def test_catalog_carries_the_pool_and_it_survives_the_manifest() -> None:
    entry = local_catalog.get("qwen3.8-flash-next")
    assert entry is not None
    assert entry.kv_pool is FLASH_NEXT_POOL
    assert entry.default_slots == FLASH_NEXT_POOL.n_slots
    assert local_catalog.pool_of(entry.served_model) is FLASH_NEXT_POOL
    assert pool_shape(asdict(entry)) == (524_288, 8)


def test_standard_entries_have_no_pool() -> None:
    standard = [m for m in local_catalog.CATALOG if m.engine == "standard"]
    assert standard
    assert all(m.kv_pool is None for m in standard)
    assert pool_shape(asdict(standard[0])) is None
    assert local_catalog.pool_of("not-a-model") is None


def test_slot_pin_names_a_role_or_nothing_at_all() -> None:
    assert slot_roles.slot_pin(SlotRole.RESEARCH) == {"slot_role": SlotRole.RESEARCH}
    assert slot_roles.slot_pin(None) == {}


def test_layout_matches_only_the_pools_own_slot_count() -> None:
    assert slot_roles.layout_matches(FLASH_NEXT_POOL, [{}] * 8)
    assert not slot_roles.layout_matches(FLASH_NEXT_POOL, [{}] * 4)
    assert not slot_roles.layout_matches(FLASH_NEXT_POOL, [])


def test_tool_call_chars_reads_a_dict_and_its_json_string_alike() -> None:
    args = {"query": "kv pool"}
    assert slot_roles.tool_call_chars("search", args) == slot_roles.tool_call_chars(
        "search", '{"query": "kv pool"}'
    )


def test_a_known_length_video_is_charged_per_merged_frame_pair() -> None:
    # 1 fps, two frames per pair, plus the extra frame ffmpeg's fps filter can emit: 60 s is
    # up to 61 frames, so 31 pairs; 59 s is 60 frames, 30 pairs.
    assert slot_roles.VIDEO_FPS == 1.0
    assert slot_roles.video_tokens_charge(60.0) == 31 * slot_roles.IMAGE_TOKENS_CHARGE
    assert slot_roles.video_tokens_charge(59.0) == 30 * slot_roles.IMAGE_TOKENS_CHARGE
    assert slot_roles.video_tokens_charge(0.5) == slot_roles.IMAGE_TOKENS_CHARGE


def test_an_unknown_length_video_is_charged_the_full_minute() -> None:
    full = slot_roles.video_tokens_charge(60.0)
    assert slot_roles.video_tokens_charge(None) == full
    assert slot_roles.video_tokens_charge(0.0) == full


def test_the_video_charge_adds_to_the_prompt_estimate() -> None:
    base = slot_roles.estimate_prompt_tokens("unknown-model", chars=3700)
    with_video = slot_roles.estimate_prompt_tokens("unknown-model", chars=3700, video_tokens=8192)
    assert with_video - base == 8192
