"""The jerv prompt-cache disk layer: every rule here answers a way v1 actually failed.

v1's post-mortem (it saved background garbage, restored stubs silently, and was inert on
the hybrids) is the module docstring of `jbrain.llm.kv_prefix`; these tests pin the
verification rules that make v2 trustworthy where v1 was hopeful."""

from pathlib import Path

import pytest

from jbrain.llm import llama_swap_config
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.local_gateway import LocalGatewayError
from jbrain.llm.types import LlmTool

PRIME = 28757  # the measured jerv prefix on gpt-oss-120b — a realistic count

TOOLS = [LlmTool(name="notes", description="read notes", input_schema={"type": "object"})]


class FakeGateway:
    """Slots + save/restore with scripted answers, recording every call."""

    def __init__(self) -> None:
        self.slot_state: list[dict[str, object]] = []
        self.save_response: dict[str, object] | Exception = {"n_saved": PRIME}
        self.restore_response: dict[str, object] | Exception = {"n_restored": PRIME}
        self.saved: list[tuple[str, int, str]] = []
        self.restored: list[tuple[str, int, str]] = []

    async def slots(self, served_model: str) -> list[dict[str, object]]:
        return self.slot_state

    async def save_slot(self, served: str, slot_id: int, filename: str) -> dict:
        self.saved.append((served, slot_id, filename))
        if isinstance(self.save_response, Exception):
            raise self.save_response
        return dict(self.save_response)

    async def restore_slot(self, served: str, slot_id: int, filename: str) -> dict:
        self.restored.append((served, slot_id, filename))
        if isinstance(self.restore_response, Exception):
            raise self.restore_response
        return dict(self.restore_response)


def _write_config(
    root: Path, served: str = "gpt-oss-120b", cmd: str = "llama-server -c 131072"
) -> None:
    (root / "llama-swap.yaml").write_text(f"models:\n  {served}:\n    cmd: {cmd}\n")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _write_config(tmp_path)
    (tmp_path / llama_swap_config.KVSLOT_DIR / "gpt-oss-120b").mkdir(parents=True)
    return tmp_path


def _store(root: Path) -> tuple[KvPrefixStore, FakeGateway]:
    gw = FakeGateway()
    return KvPrefixStore(gw, str(root)), gw  # type: ignore[arg-type]


def _slot_files(root: Path, served: str = "gpt-oss-120b") -> list[str]:
    folder = root / llama_swap_config.KVSLOT_DIR / served
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def _plant_file(root: Path, store: KvPrefixStore, served: str, system: str) -> Path:
    """The file a prior successful save would have left, at the CURRENT fingerprint."""
    fp = store._current_fingerprint(served, system, TOOLS)
    assert fp is not None
    path = root / llama_swap_config.KVSLOT_DIR / served / f"{fp}.kvslot"
    path.write_bytes(b"\0" * 64)
    return path


# ---- save -------------------------------------------------------------------------------


async def test_save_captures_only_the_slot_that_exactly_matches_the_prime(root: Path) -> None:
    """v1's fatal flaw inverted: identification is an exact integer match on the prime's own
    token count, so 'whatever held the slot' can never be what gets saved."""
    store, gw = _store(root)
    gw.slot_state = [
        {"id": 0, "n_prompt_tokens": 512, "is_processing": False},  # background residue
        {"id": 1, "n_prompt_tokens": PRIME, "is_processing": False},  # the prime
    ]
    assert await store.save_after_prime("gpt-oss-120b", "persona", TOOLS, PRIME) is True
    assert [s[1] for s in gw.saved] == [1], "must save the matching slot, not slot 0"
    assert len(_slot_files(root)) == 0  # llama-server writes the file, not the store


async def test_save_refuses_when_no_slot_matches_the_prime_count(root: Path) -> None:
    """Zero matches means something replaced the prime between the converse returning and
    the slots read — the exact race v1 lost by saving anyway."""
    store, gw = _store(root)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 512, "is_processing": False}]
    assert await store.save_after_prime("gpt-oss-120b", "persona", TOOLS, PRIME) is False
    assert gw.saved == []


async def test_save_disowns_a_file_whose_n_saved_disagrees(root: Path) -> None:
    """The server's own count is the second check: a mismatch means the slot moved under
    the save, and the file on disk is NOT the prime — it must not survive to be restored."""
    store, gw = _store(root)
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    gw.save_response = {"n_saved": 412}
    # a file the (bad) save would have produced, plus a stale one — both must go
    folder = root / llama_swap_config.KVSLOT_DIR / "gpt-oss-120b"
    (folder / "deadbeef.kvslot").write_bytes(b"junk")
    assert await store.save_after_prime("gpt-oss-120b", "persona", TOOLS, PRIME) is False
    assert _slot_files(root) == []


async def test_save_prunes_stale_fingerprints_but_keeps_the_current_one(root: Path) -> None:
    """The file itself is written SERVER-side (llama-server owns --slot-save-path); the
    store's job after a verified save is to leave exactly that file standing."""

    class _WritingGateway(FakeGateway):
        async def save_slot(self, served: str, slot_id: int, filename: str) -> dict:
            (root / llama_swap_config.KVSLOT_DIR / served / filename).write_bytes(b"\0" * 64)
            return await super().save_slot(served, slot_id, filename)

    gw = _WritingGateway()
    store = KvPrefixStore(gw, str(root))  # type: ignore[arg-type]
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    folder = root / llama_swap_config.KVSLOT_DIR / "gpt-oss-120b"
    (folder / "stalefingerprint00000000000000000.kvslot").write_bytes(b"old")
    assert await store.save_after_prime("gpt-oss-120b", "persona", TOOLS, PRIME) is True
    files = _slot_files(root)
    assert len(files) == 1 and not files[0].startswith("stale")


async def test_a_recurrent_or_unknown_model_is_never_saved(root: Path) -> None:
    """The hybrids are inert by construction (restore clears their context checkpoints) and
    an uncatalogued model has no eligibility story at all — both refuse up front."""
    store, gw = _store(root)
    gw.slot_state = [{"id": 0, "n_prompt_tokens": PRIME, "is_processing": False}]
    assert await store.save_after_prime("qwen3.8-27b-q4", "persona", TOOLS, PRIME) is False
    assert await store.save_after_prime("no-such-model", "persona", TOOLS, PRIME) is False
    assert gw.saved == []


async def test_a_prime_below_the_floor_is_not_worth_a_file(root: Path) -> None:
    store, gw = _store(root)
    assert await store.save_after_prime("gpt-oss-120b", "persona", TOOLS, 800) is False
    assert gw.saved == []


async def test_an_existing_current_file_short_circuits_the_2_gig_write(root: Path) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    assert await store.save_after_prime("gpt-oss-120b", "persona", TOOLS, PRIME) is True
    assert gw.saved == []


# ---- restore ----------------------------------------------------------------------------


async def test_restore_puts_the_prefix_back_when_no_slot_holds_it(root: Path) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    gw.slot_state = [
        {"id": 0, "n_prompt_tokens": 512, "is_processing": False},
        {"id": 1, "n_prompt_tokens": 30112, "is_processing": False},  # foreign task residue
    ]
    gw.restore_response = {"n_restored": PRIME}
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is True
    # restored into the emptier slot, never the busier one
    assert [r[1] for r in gw.restored] == [0]


async def test_restore_leaves_a_live_conversation_alone(root: Path) -> None:
    """A slot holding prefix+history reads as 'prefix present': restoring over it would
    wipe cached history to re-plant a prefix the conversation already extends."""
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    store.note_agent_turn("gpt-oss-120b", 31650)  # a real turn's prompt size
    gw.slot_state = [
        {"id": 0, "n_prompt_tokens": 512, "is_processing": False},
        {"id": 1, "n_prompt_tokens": 31650, "is_processing": False},  # the conversation
    ]
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False
    assert gw.restored == []


async def test_restore_skips_when_the_prime_itself_is_still_cached(root: Path) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    gw.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    store._prime_tokens["gpt-oss-120b"] = PRIME
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False
    assert gw.restored == []


async def test_restore_rejects_a_stub_and_falls_back_to_the_prefill(root: Path) -> None:
    """v1 restored '400s and 2 KB files' and called it success. A restore below the floor
    (or disagreeing with the known prime count) is rejected — the turn just prefills."""
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    gw.restore_response = {"n_restored": 312}
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False
    gw.restore_response = {"n_restored": PRIME - 1}
    store._prime_tokens["gpt-oss-120b"] = PRIME
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False


async def test_a_boot_restore_adopts_the_restored_count_as_the_prime_size(root: Path) -> None:
    """On a fresh process no prime has run, so the expected count is unknown — a floor-valid
    restore is accepted and its count becomes the identity future reads key on."""
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    gw.restore_response = {"n_restored": PRIME}
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is True
    assert store._prime_tokens["gpt-oss-120b"] == PRIME


async def test_restore_waits_for_an_idle_slot_rather_than_fighting_a_live_request(
    root: Path,
) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 512, "is_processing": True}]
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False
    assert gw.restored == []


async def test_no_file_for_the_current_fingerprint_means_no_restore(root: Path) -> None:
    """A stale file (different fingerprint) must never be offered: a config change moved
    the filename precisely so this lookup misses."""
    store, gw = _store(root)
    folder = root / llama_swap_config.KVSLOT_DIR / "gpt-oss-120b"
    (folder / ("ff" * 16 + ".kvslot")).write_bytes(b"stale")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False
    assert gw.restored == []


async def test_a_gateway_error_is_contained_not_raised(root: Path) -> None:
    store, gw = _store(root)
    _plant_file(root, store, "gpt-oss-120b", "persona")
    gw.slot_state = [{"id": 0, "n_prompt_tokens": 0, "is_processing": False}]
    gw.restore_response = LocalGatewayError("gateway went away")
    assert await store.restore_if_lost("gpt-oss-120b", "persona", TOOLS) is False
    gw2 = FakeGateway()
    store2 = KvPrefixStore(gw2, str(root))  # type: ignore[arg-type]
    gw2.slot_state = [{"id": 1, "n_prompt_tokens": PRIME, "is_processing": False}]
    gw2.save_response = LocalGatewayError("gateway went away")
    # a DIFFERENT prefix, so the file planted above cannot short-circuit this save
    assert await store2.save_after_prime("gpt-oss-120b", "persona-b", TOOLS, PRIME) is False


# ---- fingerprint ------------------------------------------------------------------------


def test_the_fingerprint_moves_with_launch_line_system_and_tools(tmp_path: Path) -> None:
    """Anything that can invalidate a saved slot must move the filename: the launch line
    (window/slots/extra args/build), the persona text, and the tool schema. Same inputs
    must be stable across processes, or every boot orphans the previous boot's file."""
    _write_config(tmp_path)
    store, _ = _store(tmp_path)
    base = store._current_fingerprint("gpt-oss-120b", "persona", TOOLS)
    assert base is not None
    assert store._current_fingerprint("gpt-oss-120b", "persona", TOOLS) == base
    assert store._current_fingerprint("gpt-oss-120b", "persona v2", TOOLS) != base
    other_tools = [LlmTool(name="notes", description="CHANGED", input_schema={"type": "object"})]
    assert store._current_fingerprint("gpt-oss-120b", "persona", other_tools) != base
    _write_config(tmp_path, cmd="llama-server -c 262144")
    assert store._current_fingerprint("gpt-oss-120b", "persona", TOOLS) != base


def test_no_rendered_config_means_no_fingerprint_and_no_disk_activity(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)  # no llama-swap.yaml written
    assert store._current_fingerprint("gpt-oss-120b", "persona", TOOLS) is None
