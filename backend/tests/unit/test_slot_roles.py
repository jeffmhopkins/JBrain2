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
from jbrain.llm.types import (
    AssistantMessage,
    LlmMessage,
    UserMessage,
    current_turn_start,
    replayed_steps,
)


def test_flash_next_pool_shape() -> None:
    assert FLASH_NEXT_POOL.n_ctx == 524_288
    assert FLASH_NEXT_POOL.n_slots == 10
    assert {r.role for r in FLASH_NEXT_POOL.reservations} == set(SlotRole)
    assert FLASH_NEXT_POOL.chat_pair == (SlotRole.INTERACTIVE, SlotRole.INTERACTIVE_ALT)


def test_no_cap_exceeds_the_trained_context() -> None:
    assert max(r.cap_tokens for r in FLASH_NEXT_POOL.reservations) == 262_144


def test_the_chat_pair_is_freed_last_and_small_first() -> None:
    order = FLASH_NEXT_POOL.eviction_order()
    assert order[-2:] == [0, 9], "the two chat slots hold the top two ranks"
    assert order[0] == FLASH_NEXT_POOL.slot(SlotRole.SMALL)


def test_the_slot_holding_the_latest_chat_is_freed_last_whatever_its_rank() -> None:
    # Slot 9's static rank is the top one; when slot 0 holds the most recent conversation, the
    # warm slot 9 (its prefix restorable from disk in ~2 s) goes before it.
    assert FLASH_NEXT_POOL.eviction_order(keep_last=0)[-2:] == [9, 0]
    assert FLASH_NEXT_POOL.eviction_order(keep_last=9)[-2:] == [0, 9]
    assert FLASH_NEXT_POOL.eviction_order(keep_last=42) == FLASH_NEXT_POOL.eviction_order()


def test_the_chat_pair_second_slot_is_appended_and_shares_the_interactive_cap() -> None:
    alt = FLASH_NEXT_POOL.reservation(SlotRole.INTERACTIVE_ALT)
    assert alt.slot == FLASH_NEXT_POOL.n_slots - 1 == 9
    assert alt.cap_tokens == FLASH_NEXT_POOL.cap(SlotRole.INTERACTIVE)
    assert alt.overflow is None
    assert FLASH_NEXT_POOL.in_pair(SlotRole.INTERACTIVE)
    assert FLASH_NEXT_POOL.in_pair(SlotRole.INTERACTIVE_ALT)
    assert not FLASH_NEXT_POOL.in_pair(SlotRole.SCHEDULED)
    # No caller names the second slot: every chat turn asks for the interactive role.
    assert SlotRole.INTERACTIVE_ALT not in slot_roles.TASK_ROLES.values()


def test_a_chat_pair_must_be_two_distinct_roles_with_one_cap_and_no_overflow() -> None:
    pair = (SlotRole.INTERACTIVE, SlotRole.INTERACTIVE_ALT)
    a = RoleReservation(SlotRole.INTERACTIVE, 0, 1024, 0, "a")
    b = RoleReservation(SlotRole.INTERACTIVE_ALT, 1, 1024, 1, "b")
    with pytest.raises(ValueError, match="two distinct roles"):
        KvPool(65_536, (a, b), chat_pair=(SlotRole.INTERACTIVE, SlotRole.INTERACTIVE))
    with pytest.raises(ValueError, match="two distinct roles"):
        KvPool(65_536, (a,), chat_pair=pair)
    wide = RoleReservation(SlotRole.INTERACTIVE_ALT, 1, 2048, 1, "b")
    with pytest.raises(ValueError, match="same cap"):
        KvPool(65_536, (a, wide), chat_pair=pair)
    spill = RoleReservation(SlotRole.INTERACTIVE, 0, 1024, 0, "a", SlotRole.INTERACTIVE_ALT)
    with pytest.raises(ValueError, match="no overflow"):
        KvPool(65_536, (spill, b), chat_pair=pair)
    assert KvPool(65_536, (a, b), chat_pair=pair).chat_pair == pair


def test_an_exact_pin_names_the_role_and_only_pins_when_asked() -> None:
    assert slot_roles.exact_pin(SlotRole.INTERACTIVE, False) == {"slot_role": SlotRole.INTERACTIVE}
    assert slot_roles.exact_pin(SlotRole.INTERACTIVE_ALT, True) == {
        "slot_role": SlotRole.INTERACTIVE_ALT,
        "exact_slot": True,
    }


def test_the_conversation_pin_carries_the_routing_key_beside_the_disk_key() -> None:
    assert slot_roles.conversation_pin(None) == {}
    assert slot_roles.conversation_pin("c", "s") == {"conversation_key": "c", "chat_key": "s"}
    # A firewalled chat has no disk key but is still routed by its chat.
    assert slot_roles.conversation_pin(None, "s") == {"chat_key": "s"}


def test_browse_has_its_own_slot_added_last_and_freed_early() -> None:
    """The ninth slot was appended, so the first eight keep their ids; browse steps land
    there, not in the research slot research agents use."""
    browse = FLASH_NEXT_POOL.reservation(SlotRole.BROWSE)
    assert browse.slot == 8
    assert browse.cap_tokens == FLASH_NEXT_POOL.cap(SlotRole.INGEST) == 131_072
    assert browse.overflow is None
    assert [r.role for r in FLASH_NEXT_POOL.reservations[:8]] == [
        SlotRole.INTERACTIVE,
        SlotRole.INGEST,
        SlotRole.SCHEDULED,
        SlotRole.RESEARCH,
        SlotRole.JCODE,
        SlotRole.WORKSHOP,
        SlotRole.PET,
        SlotRole.SMALL,
    ]
    assert FLASH_NEXT_POOL.eviction_order()[:2] == [7, 8]
    assert role_for("browse.step") is SlotRole.BROWSE


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
    assert pool_shape(asdict(entry)) == (524_288, 10)


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
    assert slot_roles.layout_matches(FLASH_NEXT_POOL, [{}] * 10)
    # A server still on the nine-slot layout (before its config is re-stamped) is no match.
    assert not slot_roles.layout_matches(FLASH_NEXT_POOL, [{}] * 9)
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


FLASH = "qwen3.8-flash-next"


def _replay_messages() -> list[LlmMessage]:
    return [
        UserMessage(text="earlier"),
        AssistantMessage(text="a", reasoning="old" * 10, reasoning_model=FLASH),
        UserMessage(text="now"),
        AssistantMessage(text="", reasoning="new" * 5, reasoning_model=FLASH),
        AssistantMessage(text="", reasoning="other" * 3, reasoning_model="gpt-oss-120b"),
    ]


def test_prompt_chars_counts_only_the_in_flight_reasoning_when_replayed() -> None:
    messages = _replay_messages()
    without = slot_roles.prompt_chars("s", messages, ())
    assert slot_roles.prompt_chars("s", messages, (), replay_model=FLASH) == without + 15


def test_replayed_steps_are_the_in_flight_ones_the_same_model_thought() -> None:
    messages = _replay_messages()
    assert replayed_steps(messages, FLASH) == {3}
    assert replayed_steps(messages, "gpt-oss-120b") == {4}
    assert replayed_steps(messages, "") == frozenset()


def test_prompt_chars_ignores_reasoning_a_model_is_not_sent() -> None:
    messages = _replay_messages()
    bare = [AssistantMessage(m.text) if isinstance(m, AssistantMessage) else m for m in messages]
    assert slot_roles.prompt_chars("s", messages, ()) == slot_roles.prompt_chars("s", bare, ())


def test_current_turn_start_is_just_past_the_last_user_message() -> None:
    assert current_turn_start(_replay_messages()) == 3
    assert current_turn_start([AssistantMessage(text="a")]) == 0
    assert current_turn_start([UserMessage(text="u")]) == 1


def test_only_flash_next_replays_reasoning_and_only_locally() -> None:
    assert local_catalog.replays_reasoning("local", "qwen3.8-flash-next")
    assert not local_catalog.replays_reasoning("xai", "qwen3.8-flash-next")
    assert not local_catalog.replays_reasoning("local", "qwen3.8-27b-q4")
    assert not local_catalog.replays_reasoning("local", "not-in-the-catalog")
