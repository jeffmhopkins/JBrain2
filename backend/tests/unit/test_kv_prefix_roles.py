"""The disk prefix store on a pooled model (FLASH_NEXT_ENGINE_PLAN §4b, F4): one prefix per
slot ROLE, restored into that role's own slot and never over an occupied one; the patch proven
per save; per-role memos and drift; conversation files around the interactive slot (F4c) with
role prefixes pinned above them in the budget. The gateway is faked; files are real."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from pathlib import Path
from typing import Any, cast

import pytest

from jbrain import box_events
from jbrain.llm import engine as engines
from jbrain.llm import kv_conversation, kv_prefix, llama_swap_config
from jbrain.llm.kv_conversation import ConversationHold, ConversationMeta
from jbrain.llm.kv_pool_guard import KvPoolGuard
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayError
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, SlotRole
from jbrain.llm.types import LlmTool

FLASH = "qwen3.8-flash-next"
BUILD = "b9999-869034b"
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
        self.build = BUILD

    async def props(self, served: str) -> dict[str, object]:
        return {"build_info": self.build}

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
def _roomy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store refuses a save that would leave the volume under 20 GiB free; a test's
    scratch disk is not the box's models volume."""
    monkeypatch.setattr(kv_prefix, "_free_bytes", lambda _folder: 10**13)


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


def _record_gate(root: Path, verdict: str, *, build: str = BUILD) -> None:
    """What a slot probe run leaves beside the files (api/debug `_record_restore_gate`)."""
    assert kv_prefix.write_gate_verdict(
        str(_folder(root)),
        {"fingerprint": kv_prefix.gate_fingerprint(LINE, build), "verdict": verdict},
    )


def _store(
    root: Path,
    *,
    patched: bool = True,
    conversations: bool = True,
    budget: int = 40 * 1024**3,
    gate: str | None = "passed",
) -> tuple[KvPrefixStore, FakeGateway]:
    if gate is not None:
        _record_gate(root, gate)
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


async def test_a_restored_but_unused_slot_is_not_restored_again_until_it_is_erased(
    root: Path,
) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    # Restored-but-unused reads empty, for as long as it stays unused: no re-restore.
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    # The pool guard erased it: now it is refilled.
    store.note_slot_erased(FLASH, 2)
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
    assert list(_folder(root).glob("*.kvslot*")) == [], "the unusable file is removed"
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


async def test_each_engine_has_its_own_budget(root: Path) -> None:
    """Owner, 2026-10-05: Flash-Next's files and the standard engine's never compete — a
    standard prefix past the budget neither evicts Flash-Next's nor is evicted by its save."""
    folder = _folder(root)
    flash_old = _plant(folder, "e" * 32 + ".kvslot", 600, 9e5)
    standard = _plant(
        root / llama_swap_config.KVSLOT_DIR / "gpt-oss-120b", "f" * 32 + ".kvslot", 900, 9e6
    )
    store, gw = _store(root, budget=1000)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    assert standard.exists(), "the standard engine's share is under its own budget"
    assert flash_old.exists(), "600 B + the new save fits Flash-Next's own 1000 B"
    state = await store.snapshot()
    by_engine = state["store"]["by_engine"]  # type: ignore[index]
    assert by_engine["standard"] == 900  # type: ignore[index]
    assert 600 < by_engine["flash-next"] <= 1000  # type: ignore[index,operator]
    assert state["store"]["over_budget"] is False  # type: ignore[index]

    big = _plant(folder, "g" * 32 + ".kvslot", 900, 50)  # Flash-Next's share now over
    store._prune_to_budget(keep_path=str(big))
    assert standard.exists(), "Flash-Next's prune never reaches another engine's folder"
    assert not flash_old.exists() and big.exists(), "LRU within Flash-Next's own share"


def test_the_store_budget_and_toggle_apply_live(root: Path) -> None:
    store, _gw = _store(root)
    store.configure(max_store_bytes=7 * 1024**3, conversations=False)
    assert store._max_store_bytes == 7 * 1024**3
    assert store._conversations is False


# ---- conversation decisions (pure) ---------------------------------------------------------


def test_a_saved_conversation_is_restored_on_identity_alone() -> None:
    meta = ConversationMeta(
        base="B", key=kv_conversation.key_hash("chat-A"), n_tokens=900, saved_at=0.0
    )
    assert kv_conversation.restore_decision(meta, "B", "chat-A") == "restore"
    assert kv_conversation.restore_decision(meta, "other base", "chat-A") == "base_mismatch"
    assert kv_conversation.restore_decision(meta, "B", "chat-B") == "key_mismatch"
    assert kv_conversation.restore_decision(None, "B", "chat-A") == "no_file"


def test_a_restore_is_judged_by_what_its_first_request_reused() -> None:
    assert kv_conversation.judge(30_000, 40_000) == "hit"
    assert kv_conversation.judge(6_000, 40_000) == "partial"  # more than the persona alone
    assert kv_conversation.judge(100, 40_000) == "miss"


def test_a_torn_or_foreign_claim_is_never_trusted() -> None:
    good = ConversationMeta("B", "k" * 32, 10, 1.0)
    assert ConversationMeta.from_json(good.to_json()) == good
    assert ConversationMeta.from_json("{") is None
    assert ConversationMeta.from_json('{"v": 1}') is None, "pre-review claims are not trusted"
    assert (
        ConversationMeta.from_json(good.to_json().replace('"n_tokens": 10', '"n_tokens": 0'))
        is None
    )


def test_a_hold_is_saved_only_while_the_slot_still_reads_as_it() -> None:
    hold = ConversationHold("k", "B", input_tokens=1000, output_tokens=200, dirty=True, at=0)
    assert hold.still_in_slot(1000) and hold.still_in_slot(1199)
    assert not hold.still_in_slot(999) and not hold.still_in_slot(5000)
    restored = ConversationHold("k", "B", None, None, dirty=False, at=0)
    assert not restored.still_in_slot(1000)


# ---- conversation save / restore through the store ------------------------------------------


def _turn(
    store: KvPrefixStore,
    gw: FakeGateway,
    key: str,
    n_in: int,
    *,
    cached: int = 0,
    seq: int | None = None,
    tools: tuple[str, ...] = (),
) -> None:
    """One interactive turn as the router reports it, leaving slot 0 holding prompt + answer."""
    fp = store.identity_of(FLASH, "persona", TOOLS, None)
    store.note_conversation_turn(
        FLASH,
        key,
        fingerprint=fp,
        input_tokens=n_in,
        output_tokens=300,
        cached_tokens=cached,
        seq=seq,
        tool_names=tools,
    )
    gw.slot_state = _slots(s0=n_in + 299)


async def _prepare(store: KvPrefixStore, key: str | None) -> bool:
    restored, _seq = await store.prepare_conversation(FLASH, key, "persona", TOOLS, None)
    return restored


def _claim(root: Path, name: str) -> ConversationMeta:
    meta = ConversationMeta.from_json((_folder(root) / f"{name}.meta").read_text())
    assert meta is not None
    return meta


async def test_repurposing_the_slot_saves_the_leaving_conversation_and_it_comes_back(
    root: Path,
) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)  # jerv's own prefix is on disk too
    gw.saved.clear()
    _turn(store, gw, "chat-A", 40_000)
    # Another conversation speaks: chat-A is saved off slot 0 first.
    assert not await _prepare(store, "chat-B")
    assert [s for s, _ in gw.saved] == [0]
    name = gw.saved[0][1]
    assert kv_conversation.is_conversation_file(name)
    claim = _claim(root, name)
    assert claim.key == kv_conversation.key_hash("chat-A") and claim.n_tokens == 40_299
    assert "chat-A" not in (_folder(root) / f"{name}.meta").read_text(), "only a hash on disk"
    _turn(store, gw, "chat-B", 31_000)
    # chat-A speaks again — whatever its message list looks like now: restored first.
    gw.restore_n = 40_299
    assert await _prepare(store, "chat-A")
    assert gw.restored[-1] == (0, name)
    assert store._counters.get("conversation_restored") == 1
    # The keeper's prefix restore must not overwrite the never-used slot meanwhile.
    gw.slot_state = _slots()
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)


async def test_the_first_request_after_a_restore_is_judged_and_misses_retire_the_file(
    root: Path,
) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    name = gw.saved[0][1]
    gw.restore_n = 40_299
    for attempt in range(kv_conversation.MISS_LIMIT):
        _turn(store, gw, "chat-B", 31_000)
        assert await _prepare(store, "chat-A")
        _turn(store, gw, "chat-A", 40_000, cached=200)  # reused almost nothing
        await asyncio.gather(*store._tasks)
        if attempt < kv_conversation.MISS_LIMIT - 1:
            assert _claim(root, name).misses == attempt + 1
            await _prepare(store, "chat-B")  # chat-A leaves again: re-saved, streak carried
            assert _claim(root, name).misses == attempt + 1
    assert not (_folder(root) / name).exists(), "dropped after MISS_LIMIT misses in a row"
    assert store._counters.get("conversation_restore_miss") == kv_conversation.MISS_LIMIT
    saves = len(gw.saved)
    await _prepare(store, "chat-B")
    assert len(gw.saved) == saves, "an unhelpful conversation is not written again"


async def test_a_hit_is_counted_and_resets_the_misses(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    name = gw.saved[0][1]
    kv_prefix._write_meta(
        str(_folder(root) / name), dataclasses.replace(_claim(root, name), misses=2)
    )
    _turn(store, gw, "chat-B", 31_000)
    gw.restore_n = 40_299
    assert await _prepare(store, "chat-A")
    _turn(store, gw, "chat-A", 41_000, cached=40_100)
    await asyncio.gather(*store._tasks)
    assert store._counters.get("conversation_restore_hit") == 1
    assert _claim(root, name).misses == 0


async def test_the_same_conversation_needs_nothing(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    assert not await _prepare(store, "chat-A")
    assert gw.saved == [] and gw.restored == []
    assert store._counters.get("conversation_held") == 1


async def test_a_slot_that_moved_on_is_never_saved_under_the_conversations_name(
    root: Path,
) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    gw.slot_state = _slots(s0=5_000)  # something else ran in slot 0 since
    await _prepare(store, "chat-B")
    assert gw.saved == []
    assert store._counters.get("conversation_slot_moved") == 1


async def test_a_missing_or_foreign_file_is_not_restored(root: Path) -> None:
    store, gw = _store(root)
    assert not await _prepare(store, "chat-C")
    assert store._counters.get("conversation_no_file") == 1
    assert gw.restored == []


async def test_a_restore_that_returns_the_wrong_count_drops_the_file(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    name = gw.saved[0][1]
    gw.restore_n = 12
    assert not await _prepare(store, "chat-A")
    assert not (_folder(root) / name).exists()


async def test_a_failed_restore_is_a_miss_not_a_deletion(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    name = gw.saved[0][1]
    gw.restore_error = LocalGatewayError(
        "Unable to restore slot: No available space in KV cache or invalid slot save file"
    )
    assert not await _prepare(store, "chat-A")
    assert (_folder(root) / name).exists()
    assert _claim(root, name).misses == 1


async def test_turning_the_cache_off_deletes_every_conversation_file(root: Path) -> None:
    store, gw = _store(root)
    fp = await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    store.configure(conversations=False)
    assert await store.clear_conversations() == 1
    assert [p.name for p in _folder(root).glob("*.kvslot")] == [f"{fp}.kvslot"], "prefix stays"
    _turn(store, gw, "chat-A", 40_000)
    assert not await _prepare(store, "chat-B")
    assert len(gw.saved) == 2  # the prime and the first conversation; nothing since


async def test_a_deleted_session_loses_its_files_under_every_identity(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    _turn(store, gw, "chat-B", 31_000)
    await _prepare(store, "chat-C")
    assert len(list(_folder(root).glob("c-*.kvslot"))) == 2
    assert await store.forget_conversation("chat-A") == 1
    remaining = [_claim(root, p.name).key for p in _folder(root).glob("c-*.kvslot")]
    assert remaining == [kv_conversation.key_hash("chat-B")]


async def test_a_turn_that_ran_a_location_or_mail_tool_never_reaches_disk(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")  # saved before it was tainted
    _turn(store, gw, "chat-B", 31_000)
    gw.restore_n = 40_299
    assert await _prepare(store, "chat-A"), "chat-A's file restores while it is still clean"
    _turn(store, gw, "chat-A", 41_000, tools=("current_location",))
    await asyncio.gather(*store._tasks)
    assert not list(_folder(root).glob("c-*.kvslot")) or all(
        _claim(root, p.name).key != kv_conversation.key_hash("chat-A")
        for p in _folder(root).glob("c-*.kvslot")
    )
    gw.saved.clear()
    _turn(store, gw, "chat-A", 42_000)
    await _prepare(store, "chat-B")
    assert gw.saved == [], "a tainted conversation is never saved again"


async def test_a_claim_from_a_superseded_request_is_ignored(root: Path) -> None:
    store, gw = _store(root)
    _restored, mine = await store.prepare_conversation(FLASH, "chat-A", "persona", TOOLS, None)
    await store.prepare_conversation(FLASH, "chat-B", "persona", TOOLS, None)  # took the slot
    _turn(store, gw, "chat-A", 40_000, seq=mine)
    assert FLASH not in store._conv_hold
    assert store._counters.get("conversation_claim_superseded") == 1


async def test_an_idle_conversation_is_saved_by_the_keeper_tick(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    assert not await store.save_idle_conversation(FLASH), "not idle yet"
    assert await store.save_idle_conversation(FLASH, idle_s=0)
    assert not await store.save_idle_conversation(FLASH, idle_s=0), "saved once, not every tick"
    assert len(gw.saved) == 1


async def test_an_abandoned_stream_drops_the_claim(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    store.note_conversation_abandoned(FLASH)
    await _prepare(store, "chat-B")
    assert gw.saved == []


async def test_every_save_deletes_the_old_sidecar_first_so_its_presence_is_proof(
    root: Path,
) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    name = gw.saved[0][1]
    assert (_folder(root) / f"{name}.ckpt").exists()
    gw.patched = False  # the image was rebuilt stock
    _turn(store, gw, "chat-B", 31_000)
    gw.restore_n = 40_299
    await _prepare(store, "chat-A")
    _turn(store, gw, "chat-A", 41_000, cached=40_000)
    await _prepare(store, "chat-B")
    assert store._counters.get("patch_absent") == 1
    assert store.identity_of(FLASH, "persona", TOOLS, None) is None


async def test_a_role_prefix_is_resaved_once_per_process_to_reprove_the_patch(
    root: Path,
) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    # A new process (an Update, possibly a stock rebuild) finds the file and its old sidecar.
    fresh, gw2 = _store(root, patched=False)
    gw2.slot_state = _slots(s0=PRIME)
    assert not await fresh.save_after_prime(
        FLASH, "persona", TOOLS, PRIME, role=SlotRole.INTERACTIVE
    )
    assert fresh._counters.get("patch_absent") == 1


async def test_a_save_that_would_crowd_the_volume_is_skipped(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kv_prefix, "_free_bytes", lambda _folder: 10 * 1024**3)
    store, gw = _store(root)
    gw.slot_state = _slots(s0=PRIME)
    assert not await store.save_after_prime(
        FLASH, "persona", TOOLS, PRIME, role=SlotRole.INTERACTIVE
    )
    assert gw.saved == []
    assert store._counters.get("save_skipped_low_disk") == 1


async def test_the_state_read_shows_roles_conversations_and_hit_counts(root: Path) -> None:
    store, gw = _store(root)
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    _turn(store, gw, "chat-A", 40_000)
    state: Any = await store.snapshot()
    roles = {(r["model"], r["role"]): r for r in state["roles"]}
    assert roles[(FLASH, "scheduled")]["slot"] == 2
    assert roles[(FLASH, "scheduled")]["restored_unused"] is True
    conv = state["conversations"]
    assert conv["enabled"] is True
    assert conv["held"][0]["unsaved"] is True
    assert state["summary"]["hits"] == 1


# ---- the restore gate (a passing slot probe for the running server) -------------------------


async def test_nothing_is_restored_until_a_probe_passes_but_saves_still_happen(root: Path) -> None:
    store, gw = _store(root, gate=None)
    fp = await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    assert (_folder(root) / f"{fp}.kvslot").exists(), "saves are not gated"
    gw.slot_state = _slots()
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)
    assert gw.restored == []
    assert store._counters.get("restore_gated_awaiting_probe") == 1
    state: Any = await store.snapshot([(FLASH, "persona", TOOLS, None)])
    assert state["restore_gate"] == {FLASH: "awaiting_probe"}
    # The probe passes: the next read opens the gate.
    _record_gate(root, "passed")
    store.forget_gate(FLASH)
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)


async def test_a_failed_probe_keeps_restores_off_and_says_so(root: Path) -> None:
    store, gw = _store(root, gate="failed")
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert await store.restore_gate(FLASH) == "failed"
    assert store.gate_states() == {FLASH: "failed"}


async def test_a_new_build_needs_a_new_probe(root: Path) -> None:
    store, gw = _store(root, gate="passed")
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.build = "b10000-newer"  # the image was rebuilt on another commit
    store.forget_gate()
    gw.slot_state = _slots()
    assert await store.restore_gate(FLASH) == "awaiting_probe"
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.INTERACTIVE)


async def test_a_reload_forgets_the_gate(root: Path) -> None:
    store, gw = _store(root, gate="passed")
    assert await store.restore_gate(FLASH) == "passed"
    gw.build = "b10000-newer"
    store.note_prefix_lost(FLASH)  # an unload / reload / pool resize
    assert await store.restore_gate(FLASH) == "awaiting_probe"


async def test_an_unreadable_build_reads_as_unproven(root: Path) -> None:
    store, gw = _store(root, gate="passed")

    async def _boom(served: str) -> dict[str, object]:
        raise LocalGatewayError("not resident")

    gw.props = _boom  # type: ignore[method-assign]
    assert await store.restore_gate(FLASH) == "awaiting_probe"


async def test_conversation_restores_wait_for_the_probe_too(root: Path) -> None:
    store, gw = _store(root, gate=None)
    _turn(store, gw, "chat-A", 40_000)
    await _prepare(store, "chat-B")
    assert len(gw.saved) == 1, "the leaving conversation is still saved"
    assert not await _prepare(store, "chat-A")
    assert gw.restored == []


def test_a_model_without_the_gate_has_none(root: Path) -> None:
    store, _gw = _store(root)
    assert asyncio.run(store.restore_gate("gpt-oss-120b")) is None


# ---- privacy scope ------------------------------------------------------------------------------


def test_only_chats_that_cannot_hold_firewalled_data_get_files() -> None:
    def allowed(**over: Any) -> bool:
        kw: dict[str, Any] = {
            "reads_knowledge_base": False,
            "persona_tools": frozenset({"web_search", "current_location"}),
            "domain_scopes": ("general",),
            "subject_ids": (),
            "tools_ran": ("web_search",),
        }
        kw.update(over)
        return kv_conversation.cache_allowed(**kw)

    assert allowed()
    assert allowed(domain_scopes=())
    # A Brain/curator chat reads the knowledge base: never on disk.
    assert not allowed(reads_knowledge_base=True)
    # A wildcard allowlist could reach anything; a mail-holding persona (the archivist) never.
    assert not allowed(persona_tools=None)
    assert not allowed(persona_tools=frozenset({"gmail_search"}))
    for domain in ("health", "finance", "location", "something-new"):
        assert not allowed(domain_scopes=("general", domain))
    assert not allowed(subject_ids=("s-1",))
    # Once a location, mail or records tool has run in it, never.
    for tool in ("current_location", "where_was_i", "weather", "gmail_read", "read_labs"):
        assert not allowed(tools_ran=("web_search", tool))


# ---- races against forget / clear (a claim must never outlive its conversation) ---------------


class _Parked:
    """Parks `prepare_conversation` at its engine refresh — the await between snapshotting the
    claim and saving it — so a forget or a clear can land inside that window."""

    def __init__(self, store: KvPrefixStore) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._store = store
        self._calls = 0

    async def __call__(self) -> None:
        self._calls += 1
        if self._calls == 1:
            self.entered.set()
            await self.release.wait()


async def _race(store: KvPrefixStore, interfere: Any) -> None:
    parked = _Parked(store)
    store._refresh_engine = parked  # type: ignore[method-assign]
    task = asyncio.create_task(_prepare(store, "chat-B"))
    await parked.entered.wait()
    await interfere()
    parked.release.set()
    await task


async def test_a_forget_inside_the_prepare_window_is_not_undone_by_its_save(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)
    await _race(store, lambda: store.forget_conversation("chat-A"))
    assert gw.saved == [], "a deleted chat's claim was saved after the delete"
    assert not list(_folder(root).glob("c-*.kvslot"))


async def test_a_clear_inside_the_prepare_window_is_not_undone_by_its_save(root: Path) -> None:
    store, gw = _store(root)
    _turn(store, gw, "chat-A", 40_000)

    async def turn_off() -> None:
        store.configure(conversations=False)
        await store.clear_conversations()
        store.configure(conversations=True)  # even switched straight back on

    await _race(store, turn_off)
    assert gw.saved == []


async def test_a_turn_still_streaming_at_the_delete_claims_nothing(root: Path) -> None:
    store, gw = _store(root)
    await store.forget_conversation("chat-A")  # deleted while its turn was in flight
    _turn(store, gw, "chat-A", 40_000)  # ... which then completes
    assert FLASH not in store._conv_hold
    assert not await store.save_idle_conversation(FLASH, idle_s=0)
    await _prepare(store, "chat-B")
    assert gw.saved == []


# ---- restore cells reserved in the guard's own decision ----------------------------------------


async def test_a_restores_cells_are_held_by_the_guard_while_it_streams(root: Path) -> None:
    gw = FakeGateway(_folder(root))
    guard = KvPoolGuard(gw.slots, _no_erase)
    store = KvPrefixStore(
        gw,  # type: ignore[arg-type]
        str(root),
        engine=engines.FLASH_NEXT,
        conversations=True,
        pool_guard=guard,
    )
    _record_gate(root, "passed")
    await _prime_and_save(store, gw, SlotRole.INTERACTIVE)
    gw.slot_state = _slots()
    seen: list[int] = []
    restore = gw.restore_slot

    async def watching(served: str, slot_id: int, filename: str) -> dict[str, object]:
        seen.append(guard._pending_on(FLASH, slot_id))
        return await restore(served, slot_id, filename)

    gw.restore_slot = watching  # type: ignore[method-assign]
    assert await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert seen == [PRIME], "the cells were held while the file streamed"
    assert guard._pending_on(FLASH, 2) == 0, "released after: the restored charge took over"
    assert guard._restored[(FLASH, 2)] == PRIME
    # A failed restore releases its hold and charges nothing.
    gw.restore_error = LocalGatewayError("boom")
    store.note_slot_erased(FLASH, 2)
    guard.forget_restored(FLASH)
    assert not await store.restore_if_lost(FLASH, "persona", TOOLS, role=SlotRole.SCHEDULED)
    assert guard._pending_on(FLASH, 2) == 0 and (FLASH, 2) not in guard._restored


async def _no_erase(model: str, slot: int) -> bool:
    raise AssertionError("nothing needs erasing")
