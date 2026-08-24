"""The jerv prompt-cache disk layer: every rule here answers a way a prior draft failed.

v1 (removed 2026-08-21) saved background garbage, restored stubs silently, and was inert
on the hybrids. v2's first draft was then caught by its own adversarial review comparing
two counters llama-server never makes equal — an idle slot's `n_prompt_tokens` is the
whole cache, prompt plus generated tokens, and a restored-but-unused slot reports no size
at all. The store's module docstring carries both post-mortems; these tests pin the rules
that answer them: a threshold gate instead of an equality, a restored-unused memo, exact
counts only where the max_tokens=1 prime makes them exact, poison files deleted on every
failure, and the save directory read off the launch line the server actually runs."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from jbrain import box_events
from jbrain.llm import kv_prefix, llama_swap_config, local_catalog
from jbrain.llm.kv_prefix import MIN_PREFIX_TOKENS, KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayError
from jbrain.llm.types import LlmTool

PRIME = 28757  # the measured jerv prefix on gpt-oss-120b — a realistic count

TOOLS = [LlmTool(name="notes", description="read notes", input_schema={"type": "object"})]

# The launch line's --slot-save-path is keyed by CATALOG ID, which differs from the served
# name for the VL models — the id≠served case is the one a gpt-oss-only fixture cannot see.
SERVED = "qwen3-vl-30b-a3b"
MODEL_ID = "qwen3-vl-30b"


class FakeGateway:
    """Slots + save/restore with scripted answers, recording every call. `writes_to` makes
    save_slot create the file the way the real llama-server does — the store never writes
    slot files itself."""

    def __init__(self, writes_to: Path | None = None) -> None:
        self.slot_state: list[dict[str, object]] = []
        self.save_response: dict[str, object] | Exception = {"n_saved": PRIME}
        self.restore_response: dict[str, object] | Exception = {"n_restored": PRIME}
        self.saved: list[tuple[str, int, str]] = []
        self.restored: list[tuple[str, int, str]] = []
        self._writes_to = writes_to

    async def slots(self, served_model: str) -> list[dict[str, object]]:
        return self.slot_state

    async def save_slot(self, served: str, slot_id: int, filename: str) -> dict:
        self.saved.append((served, slot_id, filename))
        if self._writes_to is not None:
            (self._writes_to / filename).write_bytes(b"\0" * 64)
        if isinstance(self.save_response, Exception):
            raise self.save_response
        return dict(self.save_response)

    async def restore_slot(self, served: str, slot_id: int, filename: str) -> dict:
        self.restored.append((served, slot_id, filename))
        if isinstance(self.restore_response, Exception):
            raise self.restore_response
        return dict(self.restore_response)


def _write_config(root: Path, *, window: int = 131072, save_path: str | None = "default") -> None:
    """A rendered config the way the generator writes it: the save path keyed by MODEL_ID
    while the model is addressed by its served name."""
    if save_path == "default":
        save_path = f"/models/{llama_swap_config.KVSLOT_DIR}/{MODEL_ID}"
    flag = f" --slot-save-path {save_path}" if save_path else ""
    (root / "llama-swap.yaml").write_text(
        f"models:\n  {SERVED}:\n    cmd: llama-server -c {window}{flag}\n"
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _write_config(tmp_path)
    (tmp_path / llama_swap_config.KVSLOT_DIR / MODEL_ID).mkdir(parents=True)
    return tmp_path


def _id_dir(root: Path) -> Path:
    return root / llama_swap_config.KVSLOT_DIR / MODEL_ID


def _store(root: Path, *, writing: bool = False) -> tuple[KvPrefixStore, FakeGateway]:
    gw = FakeGateway(writes_to=_id_dir(root) if writing else None)
    return KvPrefixStore(gw, str(root)), gw  # type: ignore[arg-type]


def _slot_files(root: Path) -> list[str]:
    folder = _id_dir(root)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _plant_file(root: Path, store: KvPrefixStore, system: str) -> Path:
    """The file a prior successful save would have left, at the CURRENT fingerprint."""
    resolved = store._resolve(SERVED, system, TOOLS, None)
    assert resolved is not None
    fingerprint, save_dir, _identity = resolved
    assert Path(save_dir) == _id_dir(root), "the store must look where the server saves"
    path = Path(save_dir) / f"{fingerprint}.kvslot"
    path.write_bytes(b"\0" * 64)
    return path


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    """Spy on the owner-visible narration — the stated point of the feature. The store
    must speak through the module CONSTANTS, or a backend typo silently unlabels the
    rows the frontend tests assume."""
    recorded: list[tuple[str, str, str | None]] = []

    async def _spy(kind: str, subject: str, *, detail: str | None = None, **kw: object) -> None:
        recorded.append((kind, subject, detail))

    monkeypatch.setattr(box_events, "record", _spy)
    return recorded


# ---- save -------------------------------------------------------------------------------


async def test_save_captures_only_the_slot_that_exactly_matches_the_prime(
    root: Path, events: list
) -> None:
    """v1's fatal flaw inverted: identification is an exact integer match on the prime's own
    token count (exact ONLY because the max_tokens=1 prime leaves the cache at precisely
    its prompt size), so 'whatever held the slot' can never be what gets saved."""
    store, gw = _store(root)
    gw.slot_state = [
        {"id": 0, "n_prompt_tokens": 512, "is_processing": False},  # background residue
        {"id": 2, "n_prompt_tokens": PRIME, "is_processing": True},  # mid-request twin
        {"id": 1, "n_prompt_tokens": PRIME, "is_processing": False},  # the prime
    ]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert [s[1] for s in gw.saved] == [1], "must save the idle matching slot only"
    assert events == [
        (box_events.KV_PREFIX_SAVED, SERVED, f"{PRIME}-token jerv prefix saved to disk")
    ]


async def test_save_refuses_when_no_idle_slot_matches_the_prime_count(root: Path) -> None:
    """Zero idle matches means something replaced the prime between the converse returning
    and the slots read — the exact race v1 lost by saving anyway. A BUSY slot at the right
    count is mid-request, in-flux state: it does not count as a match either."""
    store, gw = _store(root)
    gw.slot_state = [
        {"id": 0, "n_prompt_tokens": 512, "is_processing": False},
        {"id": 1, "n_prompt_tokens": PRIME, "is_processing": True},
    ]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is False
    assert gw.saved == []


async def test_save_disowns_a_file_whose_n_saved_disagrees(root: Path) -> None:
    """The server's own count is the second check: a mismatch means the slot moved under
    the save, and the file on disk is NOT the prime — it must not survive to be restored.
    Only THAT file: another config's cache was verified under its own count, and deleting
    it for this save's failure would re-charge a prefill the owner already paid."""
    store, gw = _store(root, writing=True)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    gw.save_response = {"n_saved": 412}
    (_id_dir(root) / "deadbeef.kvslot").write_bytes(b"junk")
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is False
    assert _slot_files(root) == ["deadbeef.kvslot"], "only the bad write is disowned"


async def test_a_save_that_errors_deletes_its_own_partial_file(root: Path) -> None:
    """A failed or timed-out save can leave a PARTIAL file at the trusted name; because an
    existing file short-circuits every future save, leaving it would poison this
    fingerprint until a config change happened to move it."""
    store, gw = _store(root, writing=True)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    gw.save_response = LocalGatewayError("timed out mid-write")
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is False
    assert _slot_files(root) == [], "the partial file must not survive"
    # ...and with the poison gone, a healthy retry succeeds where it would have
    # short-circuited against the junk.
    gw.save_response = {"n_saved": PRIME}
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True


async def test_both_slot_configs_keep_their_files_and_each_restores_its_own(
    root: Path,
) -> None:
    """The owner flips the interactive slot (1↔2), which rewrites the launch line and moves
    the fingerprint. Under the byte budget BOTH configs' caches persist — the flip that used
    to delete the other side's file and re-charge a ~2 min prefill (observed live,
    2026-08-23) now restores in ~100 ms from whichever file matches."""
    store, gw = _store(root, writing=True)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    _write_config(root, window=262144)  # the other slot config: a different launch line
    store2, gw2 = _store(root, writing=True)
    gw2.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store2.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert len(_slot_files(root)) == 2, "a config flip must not evict the other config"
    # Flip back: the ORIGINAL config's store finds its own file and restores it.
    _write_config(root)
    store3, gw3 = _store(root)
    gw3.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    assert await store3.restore_if_lost(SERVED, "persona", TOOLS) is True
    restored_name = gw3.restored[0][2]
    assert restored_name == gw.saved[0][2], "flipping back restores the matching file"


async def test_the_budget_evicts_least_recently_used_across_all_models(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past MAX_STORE_BYTES the oldest-by-mtime files go first, store-wide (another model's
    folder is the same budget), and the just-saved file survives whatever its clock says."""
    other = root / llama_swap_config.KVSLOT_DIR / "other-model"
    other.mkdir(parents=True)
    oldest = other / ("aa" * 16 + ".kvslot")
    oldest.write_bytes(b"\0" * 64)
    os.utime(oldest, (1_000, 1_000))
    middle = _id_dir(root) / ("bb" * 16 + ".kvslot")
    middle.write_bytes(b"\0" * 64)
    os.utime(middle, (2_000, 2_000))
    newest = _id_dir(root) / ("cc" * 16 + ".kvslot")
    newest.write_bytes(b"\0" * 64)
    os.utime(newest, (3_000, 3_000))
    monkeypatch.setattr(kv_prefix, "MAX_STORE_BYTES", 192)  # 4 × 64-byte files > this
    store, gw = _store(root, writing=True)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert not oldest.exists(), "least-recently-used goes first"
    assert middle.exists() and newest.exists()
    assert len(_slot_files(root)) == 3  # middle + newest + the fresh save
    # Even a budget smaller than one file never eats the file just verified.
    monkeypatch.setattr(kv_prefix, "MAX_STORE_BYTES", 1)
    saved_name = gw.saved[0][2]
    (_id_dir(root) / saved_name).unlink()
    gw.saved.clear()
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert _slot_files(root) == [saved_name], "the just-saved file always survives"


async def test_the_checkpoint_sidecar_lives_and_dies_with_its_slot_file(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The patched engine writes `<slot file>.ckpt` (deploy/patches/0001). The store
    treats the pair as one unit: the sidecar's size counts toward the budget, eviction
    removes both, an orphaned sidecar (crash between the paired removes) is swept, and
    a known-bad slot file's sidecar goes with it — a surviving sidecar would restore
    checkpoints for state that no longer exists."""
    other = root / llama_swap_config.KVSLOT_DIR / "other-model"
    other.mkdir(parents=True)
    oldest = other / ("aa" * 16 + ".kvslot")
    oldest.write_bytes(b"\0" * 64)
    oldest_ck = other / ("aa" * 16 + ".kvslot.ckpt")
    oldest_ck.write_bytes(b"\0" * 64)
    os.utime(oldest, (1_000, 1_000))
    orphan_ck = other / ("dd" * 16 + ".kvslot.ckpt")
    orphan_ck.write_bytes(b"\0" * 64)
    newest = _id_dir(root) / ("cc" * 16 + ".kvslot")
    newest.write_bytes(b"\0" * 64)
    os.utime(newest, (3_000, 3_000))
    # oldest(64) + its sidecar(64) + newest(64) + the fresh save(>0) > 200: the budget
    # must see the sidecar's bytes, or oldest+sidecar reads as under-budget and stays.
    monkeypatch.setattr(kv_prefix, "MAX_STORE_BYTES", 200)
    store, gw = _store(root, writing=True)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert not oldest.exists() and not oldest_ck.exists(), "eviction removes the pair"
    assert not orphan_ck.exists(), "an orphaned sidecar is swept"
    assert newest.exists()
    # A restore that rejects its file deletes the sidecar too.
    bad = _plant_file(root, store, "persona")
    bad_ck = Path(str(bad) + ".ckpt")
    bad_ck.write_bytes(b"\0" * 8)
    gw2_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    store2, gw2 = _store(root)
    gw2.slot_state = gw2_state
    gw2.restore_response = {"n_restored": 7}  # stub: fails the verified-size gate
    assert await store2.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert not bad.exists() and not bad_ck.exists(), "a bad slot file takes its sidecar"


async def test_a_restore_refreshes_its_files_lru_clock(root: Path) -> None:
    """A restore IS a use: it bumps the file's mtime, so the caches that keep earning their
    restores stay and the ones nothing touches age out of the budget first."""
    store, gw = _store(root)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    path = _plant_file(root, store, "persona")
    os.utime(path, (1_000, 1_000))
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True
    assert path.stat().st_mtime > 1_000, "a successful restore must touch its file"


async def test_a_recurrent_or_unknown_model_is_never_saved(root: Path) -> None:
    """A hybrid WITHOUT the catalog opt-in refuses (nemotron: unverified restore story)
    and an uncatalogued model has no eligibility story at all — both refuse up front."""
    store, gw = _store(root)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert (
        await store.save_after_prime("nemotron-3.5-lightning-30b", "persona", TOOLS, PRIME) is False
    )
    assert await store.save_after_prime("no-such-model", "persona", TOOLS, PRIME) is False
    assert gw.saved == []


async def test_the_kv_slot_restorable_flag_gates_eligibility(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `kv_slot_restorable` catalog FLAG is what admits an otherwise-excluded model
    (a hybrid/recurrent/speculative entry) to the disk layer — the mechanism, independent
    of any live opt-in. NOTE: no shipped model sets it today — the qwen3.8 twins were
    opted in on 2026-08-23 and reverted 2026-08-24 (the restore lands but the hybrid
    re-prefills with no context checkpoint; re-enable gated on the llama-server patch).
    This pins the flag's plumbing so re-enabling is a one-line flip."""
    import jbrain.llm.kv_prefix as mod

    served = "qwen3-vl-30b-a3b"  # the fixture's served model (real catalog entry)
    base = local_catalog.get_by_served(served)
    assert base is not None
    # A recurrent+speculative variant that WITHOUT the flag would be refused, WITH it is
    # eligible — the exact override the flag exists for.
    hybrid_off = replace(base, recurrent=True, kv_slot_restorable=False)
    hybrid_on = replace(base, recurrent=True, kv_slot_restorable=True)

    monkeypatch.setattr(mod.local_catalog, "get_by_served", lambda m: hybrid_off)
    store, gw = _store(root)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(served, "persona", TOOLS, PRIME) is False  # refused

    monkeypatch.setattr(mod.local_catalog, "get_by_served", lambda m: hybrid_on)
    assert await store.save_after_prime(served, "persona", TOOLS, PRIME) is True  # admitted
    assert len(gw.saved) == 1


async def test_the_patch_setting_gates_a_qwen_mtp_hybrid(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fast-Qwen-loads patch setting (patch_active) is the RUNTIME gate that admits a
    recurrent + MTP-self-drafting entry to the disk layer, replacing the reverted static
    `kv_slot_restorable` flag. With the patch off the qwen MTP-hybrid is refused (stock
    server re-prefills a restored hybrid); with it on the same entry is eligible. A plain
    recurrent entry (no MTP) stays refused whatever the setting — the patch's soundness
    argument rides on MTP self-drafting verifying every draft."""
    import jbrain.llm.kv_prefix as mod

    served = "qwen3-vl-30b-a3b"  # the fixture's served model (real catalog entry)
    base = local_catalog.get_by_served(served)
    assert base is not None
    mtp_hybrid = replace(
        base,
        recurrent=True,
        kv_slot_restorable=False,
        extra_server_args=(*base.extra_server_args, "--spec-type", "draft-mtp"),
    )
    plain_hybrid = replace(base, recurrent=True, kv_slot_restorable=False)
    monkeypatch.setattr(mod.local_catalog, "get_by_served", lambda m: mtp_hybrid)

    gw = FakeGateway(writes_to=_id_dir(root))
    gw.slot_state = [{"id": 0, "n_prompt_tokens": PRIME, "is_processing": False}]

    off = KvPrefixStore(gw, str(root), patch_active=False)  # type: ignore[arg-type]
    assert await off.save_after_prime(served, "persona", TOOLS, PRIME) is False  # refused

    on = KvPrefixStore(gw, str(root), patch_active=True)  # type: ignore[arg-type]
    assert await on.save_after_prime(served, "persona", TOOLS, PRIME) is True  # admitted
    assert len(gw.saved) == 1

    # A plain recurrent hybrid (no MTP) is refused even with the patch on — MTP is required.
    monkeypatch.setattr(mod.local_catalog, "get_by_served", lambda m: plain_hybrid)
    gw2 = FakeGateway(writes_to=_id_dir(root))
    gw2.slot_state = [{"id": 0, "n_prompt_tokens": PRIME, "is_processing": False}]
    on2 = KvPrefixStore(gw2, str(root), patch_active=True)  # type: ignore[arg-type]
    assert await on2.save_after_prime(served, "persona", TOOLS, PRIME) is False


async def test_a_model_served_without_the_flag_has_no_disk_layer(root: Path) -> None:
    """Eligibility follows the launch line itself: no --slot-save-path, no saves and no
    restores — the guard cannot drift from the flag that makes either possible."""
    _write_config(root, save_path=None)
    store, gw = _store(root)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is False
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.saved == [] and gw.restored == []


async def test_a_prime_below_the_floor_is_not_worth_a_file(root: Path) -> None:
    store, gw = _store(root)
    assert await store.save_after_prime(SERVED, "persona", TOOLS, 800) is False
    assert gw.saved == []


async def test_an_existing_current_file_short_circuits_the_2_gig_write(root: Path) -> None:
    # ...and the skipped write still refreshes the LRU clock: the prime that got here is a
    # use, and without the touch a re-primed unchanged config reads as the store's stalest.
    store, gw = _store(root)
    path = _plant_file(root, store, "persona")
    os.utime(path, (1_000, 1_000))
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert gw.saved == []
    assert path.stat().st_mtime > 1_000, "the short-circuit must touch the file it trusts"


# ---- restore ----------------------------------------------------------------------------


async def test_restore_puts_the_prefix_back_when_nothing_prefix_sized_is_cached(
    root: Path, events: list
) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.slot_state = [
        {"id": 0, "n_prompt_tokens": 512, "is_processing": False},
        {"id": 1, "n_prompt_tokens": 9000, "is_processing": False},  # bigger foreign residue
    ]
    gw.restore_response = {"n_restored": PRIME}
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True
    # restored into the emptier slot, never the busier one
    assert [r[1] for r in gw.restored] == [0]
    assert events and events[0][0] == box_events.KV_PREFIX_RESTORED
    assert f"{PRIME}-token jerv prefix restored from disk" in (events[0][2] or "")


async def test_anything_prefix_sized_blocks_the_restore_whatever_it_is(root: Path) -> None:
    """THE gate. An idle slot's n_prompt_tokens is the whole cache — prompt plus generated
    tokens — so a conversation that grew from the prefix reads BIGGER than the prime, never
    equal. Anything at or above prime size might be that conversation, and restoring over
    it would wipe cached history to re-plant a prefix it already extends. A large foreign
    prompt reads the same and is deliberately also left alone: that mistake costs one
    un-accelerated prefill, the inverse mistake costs a conversation."""
    store, gw = _store(root)
    path = _plant_file(root, store, "persona")
    os.utime(path, (1_000, 1_000))
    store._prime_tokens[SERVED] = PRIME
    for cached in (PRIME, PRIME + 240, 33000):  # prime, conversation, big foreign prompt
        gw.slot_state = [
            {"id": 0, "n_prompt_tokens": 512, "is_processing": False},
            {"id": 1, "n_prompt_tokens": cached, "is_processing": False},
        ]
        assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False, cached
    assert gw.restored == []
    # The blocked branch is the ONLY one a healthy hot config ever reaches (the keeper's
    # settled tick), so it must be the one that keeps its file fresh in the LRU budget.
    assert path.stat().st_mtime > 1_000, "a hot config's tick must refresh its file"


async def test_before_any_prime_the_floor_protects_a_long_running_servers_cache(
    root: Path,
) -> None:
    """An api restart beside a llama-server that kept a conversation cached: the fresh
    process knows no prime size, so anything substantial (>= the floor) is untouchable
    until the keeper's first prime establishes the real number."""
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": MIN_PREFIX_TOKENS + 5000, "is_processing": False}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.restored == []


async def test_a_restored_slot_reports_nothing_so_the_memo_stops_the_loop(root: Path) -> None:
    """A restored-but-unused slot has no task, hence NO n_prompt_tokens — invisible to the
    gate. Without the memo, every keeper tick would re-stream the same 2 GiB until the
    owner's first message; with it, one restore stands until a turn uses it."""
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.slot_state = [{"id": 0, "is_processing": False}]  # fresh slot: no size at all
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False, "memo must hold"
    assert len(gw.restored) == 1
    # a real turn uses the restored slot; from here the slot reports its own size
    store.note_agent_turn(SERVED, PRIME + 300)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 512, "is_processing": False}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True, (
        "after use, a genuine later loss must be restorable again"
    )


async def test_save_then_restore_compose_through_the_public_api(root: Path) -> None:
    """No private seeding: after a prime+save, the freshly primed slot must read as
    'present' to the very next probe — a store that forgot to record its own prime would
    re-restore over the state it just saved, once per process life."""
    store, gw = _store(root, writing=True)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.restored == []


async def test_restore_rejects_a_stub_deletes_the_file_and_falls_back(root: Path) -> None:
    """v1 restored '400s and 2 KB files' and called it success. A restore below the floor
    (or disagreeing with the known prime count) is rejected AND the proven-bad file is
    deleted, so the next prime's save can lay down a good one instead of being
    short-circuited by the junk's existence."""
    store, gw = _store(root)
    path = _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    gw.restore_response = {"n_restored": 312}
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert not path.exists(), "a proven-bad file must not survive to poison the fingerprint"
    path2 = _plant_file(root, store, "persona")
    gw.restore_response = {"n_restored": PRIME - 1}
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert not path2.exists()


async def test_a_boot_restore_adopts_the_restored_count_as_the_prime_size(root: Path) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    gw.slot_state = [{"id": 0, "is_processing": False}]
    gw.restore_response = {"n_restored": PRIME}
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True
    assert store._prime_tokens[SERVED] == PRIME


async def test_restore_waits_for_an_idle_slot_rather_than_fighting_a_live_request(
    root: Path,
) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 512, "is_processing": True}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.restored == []


async def test_no_file_for_the_current_fingerprint_means_no_restore(root: Path) -> None:
    store, gw = _store(root)
    (_id_dir(root) / ("ff" * 16 + ".kvslot")).write_bytes(b"stale")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.restored == []


async def test_a_gateway_error_is_contained_not_raised(root: Path) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    gw.restore_response = LocalGatewayError("gateway went away")
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    gw2 = FakeGateway()
    store2 = KvPrefixStore(gw2, str(root))  # type: ignore[arg-type]
    gw2.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    gw2.save_response = LocalGatewayError("gateway went away")
    assert await store2.save_after_prime(SERVED, "persona-b", TOOLS, PRIME) is False


# ---- identity ---------------------------------------------------------------------------


def test_the_store_looks_exactly_where_the_launch_line_says_the_server_saves(
    root: Path,
) -> None:
    """The id-vs-served split, pinned from the direction that matters: the save dir is READ
    OFF THE LAUNCH LINE (which the generator keys by catalog id) rather than re-derived
    from the served name — so the store and the server cannot disagree about the
    directory, and an operator's --slot-save-path override moves both together."""
    store, _ = _store(root)
    resolved = store._resolve(SERVED, "persona", TOOLS, None)
    assert resolved is not None and Path(resolved[1]) == _id_dir(root)
    _write_config(root, save_path="/models/.kvslots/elsewhere")
    moved = store._resolve(SERVED, "persona", TOOLS, None)
    assert moved is not None
    assert Path(moved[1]) == root / ".kvslots" / "elsewhere"


def test_the_fingerprint_moves_with_launch_line_system_and_tools(root: Path) -> None:
    """Anything that can invalidate a saved slot must move the filename: the launch line
    (window/slots/extra args/build), the persona text, and the tool schema — and the same
    inputs must be stable, or every boot orphans the previous boot's file."""
    store, _ = _store(root)

    def fp() -> str:
        resolved = store._resolve(SERVED, "persona", TOOLS, None)
        assert resolved is not None
        return resolved[0]

    base = fp()
    assert fp() == base
    r2 = store._resolve(SERVED, "persona v2", TOOLS, None)
    assert r2 is not None and r2[0] != base
    other_tools = [LlmTool(name="notes", description="CHANGED", input_schema={"type": "object"})]
    r3 = store._resolve(SERVED, "persona", other_tools, None)
    assert r3 is not None and r3[0] != base
    _write_config(root, window=262144)
    assert fp() != base


def test_no_rendered_config_means_no_identity_and_no_disk_activity(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)  # no llama-swap.yaml written
    assert store._resolve(SERVED, "persona", TOOLS, None) is None


def test_the_fingerprint_moves_with_the_reasoning_effort(root: Path) -> None:
    """The effort is part of the RENDERED prompt (gpt-oss's template writes a literal
    "Reasoning: <level>" header), so a cache saved under one effort can never match a
    prompt sent under another. If the effort didn't move the filename, changing the agent
    task's effort would restore a permanently-stale file whose save is short-circuited by
    its own existence — every reload restoring a cache no turn can use."""
    store, _ = _store(root)
    base = store._resolve(SERVED, "persona", TOOLS, None)
    low = store._resolve(SERVED, "persona", TOOLS, "low")
    high = store._resolve(SERVED, "persona", TOOLS, "high")
    assert base is not None and low is not None and high is not None
    assert len({base[0], low[0], high[0]}) == 3, "each effort keys its own file"
    assert store._resolve(SERVED, "persona", TOOLS, "low") == low  # stable per effort


async def test_a_mid_conversation_loss_restores_the_prefix_not_the_conversation(
    root: Path,
) -> None:
    """The owner's scenario (2026-08-23): a conversation several turns past the prefix,
    then an unload/reload wipes the cache. The store's job is to put the PREFIX back —
    the server's native leading-prefix match then reuses it under the conversation's
    prompt and prefills only the tail. What this pins: a lost cache mid-conversation is
    restorable (empty slot → restore fires), while a conversation still LIVE in a slot is
    never restored over (the threshold gate) — the two sides that make "restore, then
    stack the turns on top" safe."""
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    # The conversation's slot survived (cache = prefix + turns, bigger than the prime):
    # nothing to do, and restoring would wipe the turns.
    gw.slot_state = [{"id": 0, "n_prompt_tokens": PRIME + 900, "is_processing": False}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.restored == []
    # The reload wiped it (empty slot): the prefix comes back from disk, ready for the
    # next turn's prompt to extend.
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True
    assert [r[0] for r in gw.restored] == [SERVED]


async def test_a_busy_slot_is_waited_out_then_restored(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-08-24 canvas miss: the hook fired while a ~22 s vision side-call was
    0.5 s from releasing the ONLY slot, gave up silently, and the next step paid a
    204 s full re-prefill. A bounded wait catches exactly that window."""
    import jbrain.llm.kv_prefix as mod

    monkeypatch.setattr(mod, "RESTORE_BUSY_INTERVAL_S", 0.0)
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.restore_response = {"n_restored": PRIME}
    busy = [{"id": 0, "n_prompt_tokens": 5200, "is_processing": True}]
    freed = [{"id": 0, "n_prompt_tokens": 5200, "is_processing": False}]
    states = iter([busy, busy, freed, freed])
    gw.slots = lambda served: _next_state(states)  # type: ignore[method-assign]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is True
    assert len(gw.restored) == 1


def _next_state(states):  # type: ignore[no-untyped-def]
    async def _coro():  # type: ignore[no-untyped-def]
        return next(states)

    return _coro()


async def test_a_slot_still_busy_after_the_wait_is_left_alone(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jbrain.llm.kv_prefix as mod

    monkeypatch.setattr(mod, "RESTORE_BUSY_INTERVAL_S", 0.0)
    store, gw = _store(root)
    _plant_file(root, store, "persona")
    store._prime_tokens[SERVED] = PRIME
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 5200, "is_processing": True}]
    assert await store.restore_if_lost(SERVED, "persona", TOOLS) is False
    assert gw.restored == []


async def test_a_missing_file_names_the_identity_component_that_drifted(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The instrumentation the 204 s mystery needed: a save under one tool set, a
    restore attempted under another — the log must say `tools` moved, so a hidden-set
    flip is distinguishable from a race at a glance. (structlog prints to stdout.)"""
    store, gw = _store(root)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime(SERVED, "persona", TOOLS, PRIME) is True
    capsys.readouterr()
    other_tools = [LlmTool(name="extra", description="a flapped-in tool", input_schema={})]
    assert await store.restore_if_lost(SERVED, "persona", [*TOOLS, *other_tools]) is False
    out = capsys.readouterr().out
    drift = [ln for ln in out.splitlines() if "identity_drift" in ln]
    assert drift and '"tools"' in drift[0].replace("'", '"')
    assert '"changed": ["tools"]' in drift[0].replace("'", '"') or "['tools']" in drift[0]
