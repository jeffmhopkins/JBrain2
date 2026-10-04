"""The disk prefix store on a pooled model (FLASH_NEXT_ENGINE_PLAN §4b, F4): one prefix per
slot ROLE, restored into that role's own slot and never over an occupied one; the patch proven
per save; per-role memos and drift; conversation files around the interactive slot (F4c) with
role prefixes pinned above them in the budget. The gateway is faked; files are real."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, cast

import pytest

from jbrain import box_events
from jbrain.llm import engine as engines
from jbrain.llm import kv_conversation, llama_swap_config
from jbrain.llm.kv_conversation import ConversationHold, ConversationMeta
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayError
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, SlotRole
from jbrain.llm.types import AssistantMessage, LlmTool, UserMessage

FLASH = "qwen3.8-flash-next"
PRIME = 29_000
TOOLS = [LlmTool(name="notes", description="read notes", input_schema={"type": "object"})]
LINE = (
    "llama-server -m /models/qwen3.8-flash-next/x.gguf -c 524288 -np 8 --kv-unified "
    f"--slot-save-path /models/{llama_swap_config.KVSLOT_DIR}/{FLASH} --port 9001"
)


def _slots(**held: int) -> list[dict[str, object]]:
    """Eight idle slots; `held` maps 's<id>' to that slot's cached token count."""
    return [
        {"id": i, "is_processing": False, "n_prompt_tokens": held.get(f"s{i}", 0)}
        for i in range(FLASH_NEXT_POOL.n_slots)
    ]


class FakeGateway:
    """Save writes the slot file the way llama-server does, and — when `patched` — the
    checkpoint sidecar the patched build writes beside it on every save."""

    def __init__(self, folder: Path, *, patched: bool = True) -> None:
        self.folder = folder
        self.patched = patched
        self.slot_state = _slots()
        self.save_n: int | None = None
        self.restore_n: int | None = None
        self.restore_error: Exception | None = None
        self.saved: list[tuple[int, str]] = []
        self.restored: list[tuple[int, str]] = []

    async def slots(self, served: str) -> list[dict[str, object]]:
        return [dict(s) for s in self.slot_state]

    async def save_slot(self, served: str, slot_id: int, filename: str) -> dict[str, object]:
        self.saved.append((slot_id, filename))
        header = (0).to_bytes(4, "little") * 2 + PRIME.to_bytes(4, "little")
        (self.folder / filename).write_bytes(header + b"\0" * 52)
        if self.patched:
            (self.folder / f"{filename}.ckpt").write_bytes(b"JBCK")
        n = self.save_n
        if n is None:
            n = next(
                (
                    int(cast(int, s["n_prompt_tokens"]))
                    for s in self.slot_state
                    if s["id"] == slot_id
                ),
                0,
            )
        return {"n_saved": n}

    async def restore_slot(self, served: str, slot_id: int, filename: str) -> dict[str, object]:
        self.restored.append((slot_id, filename))
        if self.restore_error is not None:
            raise self.restore_error
        return {"n_restored": self.restore_n if self.restore_n is not None else PRIME}


@pytest.fixture(autouse=True)
def _quiet_events(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(box_events, "record", _noop)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / engines.CONFIG_FILE[engines.FLASH_NEXT]).write_text(
        f"models:\n  {FLASH}:\n    cmd: {LINE}\n"
    )
    (tmp_path / llama_swap_config.KVSLOT_DIR / FLASH).mkdir(parents=True)
    return tmp_path


def _folder(root: Path) -> Path:
    return root / llama_swap_config.KVSLOT_DIR / FLASH


def _store(
    root: Path, *, patched: bool = True, conversations: bool = True, budget: int = 40 * 1024**3
) -> tuple[KvPrefixStore, FakeGateway]:
    gw = FakeGateway(_folder(root), patched=patched)
    store = KvPrefixStore(
        gw,  # type: ignore[arg-type]
        str(root),
        engine=engines.FLASH_NEXT,
        conversations=conversations,
        max_store_bytes=budget,
    )
    return store, gw


async def _prime_and_save(store: KvPrefixStore, gw: FakeGateway, role: SlotRole) -> str:
    gw.slot_state = _slots(**{f"s{FLASH_NEXT_POOL.slot(role)}": PRIME})
    assert await store.save_after_prime(FLASH, "persona", TOOLS, PRIME, role=role)
    fp = store.identity_of(FLASH, "persona", TOOLS, None)
    assert fp is not None
    return fp


# ---- per-role fingerprints and slots ------------------------------------------------------


def test_each_roles_effort_moves_its_fingerprint_and_a_shared_identity_shares_the_file(
    root: Path,
) -> None:
    store, _gw = _store(root)
    jerv_low = store.identity_of(FLASH, "persona", TOOLS, "low")
    jerv_high = store.identity_of(FLASH, "persona", TOOLS, "high")
    research = store.identity_of(FLASH, "research base", TOOLS, "low")
    assert len({jerv_low, jerv_high, research}) == 3
    # Scheduled turns send jerv's prefix at the agent task's effort: the same identity, so the
    # same file serves both slots.
    assert store.identity_of(FLASH, "persona", TOOLS, "low") == jerv_low


async def test_a_prime_is_saved_only_from_its_roles_own_slot(root: Path) -> None:
    store, gw = _store(root)
    # The same count in the research slot is a coincidence, not the prime.
    gw.slot_state = _slots(s3=PRIME)
    assert not await store.save_after_prime(
        FLASH, "persona", TOOLS, PRIME, role=SlotRole.INTERACTIVE
    )
    gw.slot_state = _slots(s0=PRIME, s3=PRIME)
    assert await store.save_after_prime(FLASH, "persona", TOOLS, PRIME, role=SlotRole.INTERACTIVE)
    assert [s for s, _ in gw.saved] == [0]


async def test_restore_targets_the_roles_slot(root: Path) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots(s0=PRIME + 900)  # jerv's slot holds a live conversation
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert [s for s, _ in gw.restored] == [2]


async def test_an_occupied_role_slot_is_never_overwritten(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jbrain.llm.kv_prefix as mod

    monkeypatch.setattr(mod, "RESTORE_BUSY_INTERVAL_S", 0.0)
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    # Even a few hundred tokens of the role's own traffic is that role's cache.
    gw.slot_state = _slots(s2=300)
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    gw.slot_state = _slots()
    gw.slot_state[2]["is_processing"] = True
    gw.slot_state[2]["n_prompt_tokens"] = 0
    store_busy = await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert not store_busy
    assert gw.restored == []


async def test_a_role_restore_never_crowds_the_pool(root: Path) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    slots = _slots()
    # Research and jcode busy prefilling, each charged its whole cap: the pool is spoken for.
    for busy in (3, 4):
        slots[busy]["is_processing"] = True
        slots[busy]["n_prompt_tokens"] = 1000
        slots[busy]["next_token"] = [{"n_decoded": 0, "n_remain": 4096}]
    slots[1]["n_prompt_tokens"] = 1000
    gw.slot_state = slots
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)
    assert store._counters.get("restore_skipped_pool_full") == 1
    assert gw.restored == []


async def test_a_pooled_memo_expires_so_an_erased_slot_is_refilled(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jbrain.llm.kv_prefix as mod

    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    # Restored-but-unused reads empty: the memo stops a re-restore every tick...
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    # ...but the pool guard may have erased it unseen, so the memo only rate-limits.
    monkeypatch.setattr(mod, "POOLED_RESTORE_MEMO_S", -1.0)
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert [s for s, _ in gw.restored] == [2, 2]


async def test_a_role_switch_does_not_read_as_identity_drift(root: Path) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots(s0=PRIME)
    # A research turn's own prompt has no file; that is not jerv's identity drifting.
    assert not await store.restore_if_lost(FLASH, "research base", TOOLS, role=SlotRole.RESEARCH)
    assert "identity_drift" not in store._counters
    # jerv's own identity moving still says so.
    assert not await store.restore_if_lost(FLASH, "persona v2", TOOLS, role=SlotRole.INTERACTIVE)
    assert store._counters.get("identity_drift") == 1


async def test_each_roles_memo_is_retired_by_its_own_turn(root: Path) -> None:
    store, gw = _store(root)
    fp = await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    store.note_agent_turn(FLASH, PRIME + 10, fingerprint=fp, role=SlotRole.SCHEDULED)
    assert (FLASH, SlotRole.SCHEDULED) not in store._restored_unused
    assert (FLASH, SlotRole.INTERACTIVE) in store._restored_unused


# ---- the patch gate --------------------------------------------------------------------------


async def test_a_save_without_its_sidecar_proves_the_build_unpatched(root: Path) -> None:
    store, gw = _store(root, patched=False)
    gw.slot_state = _slots(s0=PRIME)
    assert not await store.save_after_prime(
        FLASH, "persona", TOOLS, PRIME, role=SlotRole.INTERACTIVE
    )
    assert list(_folder(root).iterdir()) == [], "the unusable file is removed"
    assert store.identity_of(FLASH, "persona", TOOLS, None) is None, "out of the disk layer"
    assert "checkpoint-sidecar patch" in store._ineligible_reason(FLASH)
    assert store._counters.get("patch_absent") == 1


async def test_a_patched_save_admits_the_model(root: Path) -> None:
    store, gw = _store(root, patched=True)
    fp = await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    assert (_folder(root) / f"{fp}.kvslot.ckpt").exists()


async def test_a_file_without_a_sidecar_is_never_restored(root: Path) -> None:
    store, gw = _store(root)
    fp = await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    (_folder(root) / f"{fp}.kvslot.ckpt").unlink()
    gw.slot_state = _slots()
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)
    assert gw.restored == []


# ---- engine switch and budget ------------------------------------------------------------


def _plant(folder: Path, name: str, size: int, age_s: float) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\0" * size)
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


async def test_an_engine_switch_leaves_the_other_engines_files(root: Path) -> None:
    gpt = _plant(
        root / llama_swap_config.KVSLOT_DIR / "gpt-oss-120b", "a" * 32 + ".kvslot", 64, 9e5
    )
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    store.set_engine(engines.STANDARD)  # the switch back: Flash-Next's line is no longer read
    assert store.identity_of(FLASH, "persona", TOOLS, None) is None
    assert gpt.exists()
    assert len(list(_folder(root).glob("*.kvslot"))) == 1


async def test_the_budget_evicts_conversations_before_any_role_prefix(root: Path) -> None:
    folder = _folder(root)
    old_prefix = _plant(folder, "b" * 32 + ".kvslot", 500, 9e5)  # oldest file of all
    newer_conv = _plant(folder, kv_conversation.file_name("x", "chat-1"), 500, 10)
    (folder / (newer_conv.name + ".meta")).write_text("{}")
    store, gw = _store(root, budget=1000)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)  # ~64 B + sidecar, pushes it over
    assert old_prefix.exists(), "role prefixes are pinned above conversation files"
    assert not newer_conv.exists()
    assert not (folder / (newer_conv.name + ".meta")).exists(), "the claim goes with its file"


async def test_past_the_conversations_role_prefixes_go_by_lru(root: Path) -> None:
    folder = _folder(root)
    oldest = _plant(folder, "c" * 32 + ".kvslot", 700, 9e5)
    newer = _plant(folder, "d" * 32 + ".kvslot", 300, 100)
    store, gw = _store(root, budget=1000)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    assert not oldest.exists() and newer.exists()


def test_the_store_budget_and_toggle_apply_live(root: Path) -> None:
    store, _gw = _store(root)
    store.configure(max_store_bytes=7 * 1024**3, conversations=False)
    assert store._max_store_bytes == 7 * 1024**3
    assert store._conversations is False


# ---- conversation decisions (pure) ---------------------------------------------------------


def _msgs(*texts: str) -> list[UserMessage | AssistantMessage]:
    out: list[UserMessage | AssistantMessage] = []
    for i, text in enumerate(texts):
        out.append(UserMessage(text) if i % 2 == 0 else AssistantMessage(text=text))
    return out


def test_a_saved_conversation_restores_only_when_its_messages_open_the_prompt() -> None:
    saved = kv_conversation.message_digests(_msgs("hi", "hello"))
    meta = ConversationMeta(base="B", prefix=saved, n_tokens=900, saved_at=0.0)
    longer = kv_conversation.message_digests(_msgs("hi", "hello", "and now?"))
    assert kv_conversation.restore_decision(meta, "B", longer) == "restore"
    assert kv_conversation.restore_decision(meta, "B", saved) == "restore", "a retry"
    edited = kv_conversation.message_digests(_msgs("hey", "hello", "and now?"))
    assert kv_conversation.restore_decision(meta, "B", edited) == "prefix_mismatch"
    shorter = kv_conversation.message_digests(_msgs("hi"))
    assert kv_conversation.restore_decision(meta, "B", shorter) == "prefix_mismatch"
    assert kv_conversation.restore_decision(meta, "other base", longer) == "base_mismatch"
    assert kv_conversation.restore_decision(None, "B", longer) == "no_file"


def test_digests_chain_so_one_entry_covers_the_whole_prefix() -> None:
    a = kv_conversation.message_digests(_msgs("one", "two", "three"))
    b = kv_conversation.message_digests(_msgs("ONE", "two", "three"))
    assert a[1:] != b[1:], "a change at the head moves every later digest"


def test_a_torn_or_foreign_claim_is_never_trusted() -> None:
    good = ConversationMeta("B", ("d1",), 10, 1.0)
    assert ConversationMeta.from_json(good.to_json()) == good
    assert ConversationMeta.from_json("{") is None
    assert ConversationMeta.from_json('{"v": 99}') is None
    assert (
        ConversationMeta.from_json(good.to_json().replace('"n_tokens": 10', '"n_tokens": 0'))
        is None
    )


def test_a_hold_is_saved_only_while_the_slot_still_reads_as_it() -> None:
    hold = ConversationHold(
        "k", "B", ("d",), input_tokens=1000, output_tokens=200, dirty=True, at=0
    )
    assert hold.still_in_slot(1000) and hold.still_in_slot(1199)
    assert not hold.still_in_slot(999) and not hold.still_in_slot(5000)
    restored = ConversationHold("k", "B", ("d",), None, None, dirty=False, at=0)
    assert not restored.still_in_slot(1000)


# ---- conversation save / restore through the store ------------------------------------------


async def _turn(store: KvPrefixStore, gw: FakeGateway, key: str, msgs: list, n_in: int) -> None:
    """One interactive turn as the router reports it, leaving slot 0 holding prompt + answer."""
    fp = store.identity_of(FLASH, "persona", TOOLS, None)
    store.note_conversation_turn(
        FLASH, key, msgs, fingerprint=fp, input_tokens=n_in, output_tokens=300
    )
    gw.slot_state = _slots(s0=n_in + 299)


async def test_repurposing_the_slot_saves_the_leaving_conversation_and_it_comes_back(
    root: Path,
) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)  # jerv's own prefix is on disk too
    gw.saved.clear()
    a1 = _msgs("tell me about stones")
    await _turn(store, gw, "chat-A", a1, 40_000)
    # Another conversation speaks: chat-A is saved off slot 0 first.
    assert not await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    assert [s for s, _ in gw.saved] == [0]
    name = gw.saved[0][1]
    assert kv_conversation.is_conversation_file(name)
    assert (_folder(root) / f"{name}.meta").exists()
    await _turn(store, gw, "chat-B", _msgs("x"), 31_000)
    # chat-A speaks again with its transcript extended: restored into slot 0 before the request.
    gw.restore_n = 40_299
    a2 = [*a1, AssistantMessage(text="they are old"), UserMessage("how old?")]
    assert await store.prepare_conversation(FLASH, "chat-A", "persona", TOOLS, None, a2)
    assert gw.restored[-1] == (0, name)
    assert store._counters.get("conversation_restored") == 1
    # The keeper's prefix restore must not overwrite the never-used slot meanwhile.
    gw.slot_state = _slots()
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)


async def test_the_same_conversation_needs_nothing(root: Path) -> None:
    store, gw = _store(root)
    await _turn(store, gw, "chat-A", _msgs("q"), 40_000)
    assert not await store.prepare_conversation(
        FLASH, "chat-A", "persona", TOOLS, None, _msgs("q", "a", "q2")
    )
    assert gw.saved == [] and gw.restored == []
    assert store._counters.get("conversation_held") == 1


async def test_a_slot_that_moved_on_is_never_saved_under_the_conversations_name(
    root: Path,
) -> None:
    store, gw = _store(root)
    await _turn(store, gw, "chat-A", _msgs("q"), 40_000)
    gw.slot_state = _slots(s0=5_000)  # something else ran in slot 0 since
    await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    assert gw.saved == []
    assert store._counters.get("conversation_slot_moved") == 1


async def test_a_mismatched_or_missing_file_is_not_restored(root: Path) -> None:
    store, gw = _store(root)
    a1 = _msgs("q")
    await _turn(store, gw, "chat-A", a1, 40_000)
    await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    name = gw.saved[0][1]
    # chat-A comes back EDITED: its file can never match again, and goes.
    edited = _msgs("q (edited)", "a", "next")
    assert not await store.prepare_conversation(FLASH, "chat-A", "persona", TOOLS, None, edited)
    assert gw.restored == []
    assert not (_folder(root) / name).exists()
    # A conversation never saved has nothing to restore.
    assert not await store.prepare_conversation(FLASH, "chat-C", "persona", TOOLS, None, a1)
    assert store._counters.get("conversation_no_file", 0) >= 1


async def test_a_restore_that_returns_the_wrong_count_drops_the_file(root: Path) -> None:
    store, gw = _store(root)
    a1 = _msgs("q")
    await _turn(store, gw, "chat-A", a1, 40_000)
    await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    name = gw.saved[0][1]
    gw.restore_n = 12
    assert not await store.prepare_conversation(FLASH, "chat-A", "persona", TOOLS, None, a1)
    assert not (_folder(root) / name).exists()


async def test_a_full_pool_keeps_the_file_for_next_time(root: Path) -> None:
    store, gw = _store(root)
    a1 = _msgs("q")
    await _turn(store, gw, "chat-A", a1, 40_000)
    await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    name = gw.saved[0][1]
    gw.restore_error = LocalGatewayError("Unable to restore slot: No available space in KV cache")
    assert not await store.prepare_conversation(FLASH, "chat-A", "persona", TOOLS, None, a1)
    assert (_folder(root) / name).exists()


async def test_the_toggle_off_saves_and_restores_nothing(root: Path) -> None:
    store, gw = _store(root, conversations=False)
    await _turn(store, gw, "chat-A", _msgs("q"), 40_000)
    assert not await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    assert gw.saved == []


async def test_an_idle_conversation_is_saved_by_the_keeper_tick(root: Path) -> None:
    store, gw = _store(root)
    await _turn(store, gw, "chat-A", _msgs("q"), 40_000)
    assert not await store.save_idle_conversation(FLASH), "not idle yet"
    assert await store.save_idle_conversation(FLASH, idle_s=0)
    assert not await store.save_idle_conversation(FLASH, idle_s=0), "saved once, not every tick"
    assert len(gw.saved) == 1


async def test_an_abandoned_stream_drops_the_claim(root: Path) -> None:
    store, gw = _store(root)
    await _turn(store, gw, "chat-A", _msgs("q"), 40_000)
    store.note_conversation_abandoned(FLASH)
    await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None, _msgs("x"))
    assert gw.saved == []


async def test_the_state_read_shows_roles_conversations_and_hit_counts(root: Path) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    await _turn(store, gw, "chat-A", _msgs("q"), 40_000)
    state: Any = await store.snapshot()
    roles = {(r["model"], r["role"]): r for r in state["roles"]}
    assert roles[(FLASH, "scheduled")]["slot"] == 2
    assert roles[(FLASH, "scheduled")]["restored_unused"] is True
    conv = state["conversations"]
    assert conv["enabled"] is True
    assert conv["held"][0]["unsaved"] is True
    assert state["summary"]["hits"] == 1
