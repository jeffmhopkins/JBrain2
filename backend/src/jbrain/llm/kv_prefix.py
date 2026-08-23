"""Disk save/restore of the agent-turn model's primed prefix — the jerv prompt cache.

The interactive model's first-token latency is owned by a ~29k-token persona+tools prefill
(~60 s measured on gpt-oss-120b). The WarmKeeper keeps that prefix hot in a slot, and a
dedicated second slot keeps background traffic from evicting it — but both protections live
in RAM: a restart pays the prefill again, and a single-slot configuration (fewer slots is
the first thing an operator cuts when memory is tight) loses the prefix to any background
task. This store is the durable base layer under both: after the keeper primes, the slot's
KV state is saved to disk once; whenever the prefix is later found missing, it is restored
in ~2 s instead of re-read in ~60 (measured 2026-08-21: a 27,787-token slot back in ~90 ms
page-cache-warm).

WHY V1 OF THIS IDEA WAS REMOVED, AND WHAT EACH LESSON PINS HERE (the old code's own
post-mortem lives at `warm_keeper.py`'s prime step):

  - It saved garbage: with one slot shared by background traffic, "save the slot" captured
    whatever held it — 400s and 2 KB files. Here a save happens ONLY when a slot's
    `n_prompt_tokens` exactly equals the prime's own `usage.input_tokens`, read in the same
    breath as the prime — an integer match no unrelated request plausibly satisfies —
    and the server's `n_saved` must equal it again or the file is deleted.
  - It restored garbage silently. Here `n_restored` is verified against the same count and
    a floor, and any shortfall falls back to the prefill the restore was replacing.
  - It was inert-by-construction on the recurrent hybrids (restore clears the context
    checkpoints that are their only prefix-reuse mechanism) — they are refused up front,
    as are speculative entries, whose draft state no slot file captures.
  - Its files could only be pruned by a deploy. Here each save prunes its model's stale
    fingerprints, so the cost is one ~2 GiB file per model, not a graveyard.

The FINGERPRINT is the validity rule: sha256 over the model's rendered launch line (read
back from llama-swap.yaml — the same source `served_shape_from_config` trusts, because it
cannot disagree with what the server executes) plus the exact system text and tool schema
the prime sent. Any change that could make a saved state stale — window, slots, extra
args, a new build's flags, a persona or tool edit — moves the filename, and the stale file
is pruned on the next save. Everything here is best-effort: the worst case of any failure
is the prefill that would have happened anyway.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Sequence

import structlog

from jbrain import box_events
from jbrain.llm import llama_swap_config, local_catalog
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.types import LlmTool

log = structlog.get_logger()

# The task whose prefix is worth a disk file — the interactive chat turn. Spelled here
# (not imported from warm_keeper) so the router can depend on this module alone.
AGENT_TURN_TASK = "agent.turn"

# A restore below this many tokens restored nothing worth having: the jerv prefix is
# ~29k tokens, and v1's garbage files restored a few hundred. Falling back to the
# prefill is strictly better than trusting a stub.
MIN_PREFIX_TOKENS = 4096

_SLOT_FILE_SUFFIX = ".kvslot"


def _slot_int(slot: dict[str, object], key: str) -> int:
    value = slot.get(key)
    return value if isinstance(value, int) else 0


def _fingerprint(launch_line: str, system: str, tools: Sequence[LlmTool]) -> str:
    tool_blob = json.dumps(
        [{"name": t.name, "description": t.description, "schema": t.input_schema} for t in tools],
        sort_keys=True,
    )
    digest = hashlib.sha256()
    for part in (launch_line, system, tool_blob):
        digest.update(part.encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


class KvPrefixStore:
    """One per process, wired beside the gateway. `models_root` is THIS process's view of
    the models volume (`settings.local_models_dir`); llama-server's view (`/models/…`) is
    rendered into `--slot-save-path` by the config generator, and the two meet at the same
    per-model directory."""

    def __init__(self, gateway: LocalGatewayClient, models_root: str) -> None:
        self._gateway = gateway
        self._models_root = models_root
        # The prime's exact token count per served model — the integer that identifies
        # the primed slot among /slots entries, and the expected restore size.
        self._prime_tokens: dict[str, int] = {}
        # Recent agent-turn prompt sizes per model: a slot holding a live jerv
        # CONVERSATION (prefix + history) must read as "prefix present", or a restore
        # would wipe cached history to re-plant a prefix the conversation already extends.
        self._turn_tokens: dict[str, list[int]] = {}
        # One restore at a time: a keeper tick and an inbound turn discovering the same
        # loss must not both stream the file into different slots.
        self._lock = asyncio.Lock()

    # ---- identity -------------------------------------------------------------------

    def _eligible(self, served_model: str) -> local_catalog.LocalModel | None:
        model = local_catalog.get_by_served(served_model)
        if model is None or model.recurrent or model.is_speculative:
            return None
        return model

    def _file_for(self, served_model: str, fingerprint: str) -> str:
        return os.path.join(
            self._models_root,
            llama_swap_config.KVSLOT_DIR,
            served_model,
            f"{fingerprint}{_SLOT_FILE_SUFFIX}",
        )

    def _current_fingerprint(
        self, served_model: str, system: str, tools: Sequence[LlmTool]
    ) -> str | None:
        line = llama_swap_config.launch_line(self._models_root, served_model)
        if line is None:
            return None
        return _fingerprint(line, system, tools)

    def note_agent_turn(self, served_model: str, input_tokens: int) -> None:
        """Record a real jerv turn's prompt size, so the slot it grew is recognised as
        holding the prefix. A short tail is enough — only sizes still plausibly cached
        in some slot matter, and a re-prime resets the world anyway."""
        if input_tokens <= 0:
            return
        sizes = self._turn_tokens.setdefault(served_model, [])
        sizes.append(input_tokens)
        del sizes[:-8]

    def _known_sizes(self, served_model: str) -> set[int]:
        known = set(self._turn_tokens.get(served_model, ()))
        prime = self._prime_tokens.get(served_model)
        if prime is not None:
            known.add(prime)
        return known

    # ---- save -----------------------------------------------------------------------

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: Sequence[LlmTool],
        prime_tokens: int,
    ) -> bool:
        """Persist the freshly primed slot, called by the keeper in the same breath as a
        successful prime. Returns True when a valid file exists afterwards (already
        present counts). Best-effort: every failure is a log line, never an exception."""
        if self._eligible(served_model) is None or prime_tokens < MIN_PREFIX_TOKENS:
            return False
        self._prime_tokens[served_model] = prime_tokens
        fingerprint = self._current_fingerprint(served_model, system, tools)
        if fingerprint is None:
            return False
        path = self._file_for(served_model, fingerprint)
        if await asyncio.to_thread(os.path.exists, path):
            return True  # this exact prefix is already on disk — a save is a 2 GiB write
        try:
            slots = await self._gateway.slots(served_model)
        except LocalGatewayError as exc:
            log.info("kv_prefix.slots_unreadable", model=served_model, error=str(exc))
            return False
        matches = [
            s
            for s in slots
            if isinstance(s, dict)
            and s.get("n_prompt_tokens") == prime_tokens
            and not s.get("is_processing")
        ]
        if len(matches) != 1:
            # Zero: something replaced the prime between the converse returning and this
            # read — the exact race v1 lost by saving anyway. More than one: ambiguous.
            log.info(
                "kv_prefix.slot_unidentified",
                model=served_model,
                expected_tokens=prime_tokens,
                candidates=len(matches),
            )
            return False
        slot_id = _slot_int(matches[0], "id")
        try:
            resp = await self._gateway.save_slot(
                served_model, slot_id, f"{fingerprint}{_SLOT_FILE_SUFFIX}"
            )
        except LocalGatewayError as exc:
            log.info("kv_prefix.save_failed", model=served_model, error=str(exc))
            return False
        n_saved = resp.get("n_saved")
        if n_saved != prime_tokens:
            # The slot moved under the save, or the server saved something else. The file
            # on disk is NOT the prime — remove it, or a later restore trusts it.
            log.warning(
                "kv_prefix.save_mismatch",
                model=served_model,
                expected=prime_tokens,
                n_saved=n_saved,
            )
            await asyncio.to_thread(self._prune, served_model, None)
            return False
        await asyncio.to_thread(self._prune, served_model, f"{fingerprint}{_SLOT_FILE_SUFFIX}")
        await box_events.record(
            box_events.KV_PREFIX_SAVED,
            served_model,
            detail=f"{prime_tokens}-token jerv prefix saved to disk",
        )
        log.info("kv_prefix.saved", model=served_model, tokens=prime_tokens, slot=slot_id)
        return True

    def _prune(self, served_model: str, keep: str | None) -> None:
        """Runs in a thread (asyncio.to_thread) — plain blocking fs on purpose."""
        folder = os.path.join(self._models_root, llama_swap_config.KVSLOT_DIR, served_model)
        try:
            for name in os.listdir(folder):
                if name.endswith(_SLOT_FILE_SUFFIX) and name != keep:
                    os.remove(os.path.join(folder, name))
        except OSError:
            pass  # a missing dir or a busy file is not worth failing a save over

    # ---- restore --------------------------------------------------------------------

    async def restore_if_lost(
        self, served_model: str, system: str, tools: Sequence[LlmTool]
    ) -> bool:
        """Put the prefix back if no slot currently holds it and a valid file exists.
        Returns True only when a verified restore happened. Safe to call eagerly: when
        the prefix (or a conversation grown from it) is live in any slot, this is one
        /slots read and no writes."""
        if self._eligible(served_model) is None:
            return False
        fingerprint = self._current_fingerprint(served_model, system, tools)
        if fingerprint is None:
            return False
        if not await asyncio.to_thread(os.path.exists, self._file_for(served_model, fingerprint)):
            return False
        async with self._lock:
            try:
                slots = await self._gateway.slots(served_model)
            except LocalGatewayError as exc:
                log.info("kv_prefix.slots_unreadable", model=served_model, error=str(exc))
                return False
            known = self._known_sizes(served_model)
            occupied = [s for s in slots if isinstance(s, dict)]
            if any(s.get("n_prompt_tokens") in known for s in occupied):
                return False  # the prefix (or a conversation extending it) is already live
            idle = [s for s in occupied if not s.get("is_processing")]
            if not idle:
                return False  # every slot busy — restoring now would fight a live request
            # Prefer an empty slot; else the one holding the smallest foreign prompt.
            target = min(idle, key=lambda s: _slot_int(s, "n_prompt_tokens"))
            slot_id = _slot_int(target, "id")
            started = time.perf_counter()
            try:
                resp = await self._gateway.restore_slot(
                    served_model, slot_id, f"{fingerprint}{_SLOT_FILE_SUFFIX}"
                )
            except LocalGatewayError as exc:
                log.info("kv_prefix.restore_failed", model=served_model, error=str(exc))
                return False
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            n_restored = resp.get("n_restored")
            expected = self._prime_tokens.get(served_model)
            if (
                not isinstance(n_restored, int)
                or n_restored < MIN_PREFIX_TOKENS
                or (expected is not None and n_restored != expected)
            ):
                # A stub or a partial read: the slot now holds junk, but the next request
                # simply misses the cache and prefills — the exact behaviour without this
                # store. Log it loudly; a repeat means the file is bad, and the next
                # successful save replaces it.
                log.warning(
                    "kv_prefix.restore_rejected",
                    model=served_model,
                    n_restored=n_restored,
                    expected=expected,
                )
                return False
            if expected is None:
                # First restore of this process life (a boot): adopt the restored count as
                # the prime size so slot identification works before any prime has run.
                self._prime_tokens[served_model] = n_restored
            await box_events.record(
                box_events.KV_PREFIX_RESTORED,
                served_model,
                detail=f"{n_restored}-token jerv prefix restored from disk in {elapsed_ms} ms",
            )
            log.info(
                "kv_prefix.restored",
                model=served_model,
                tokens=n_restored,
                slot=slot_id,
                elapsed_ms=elapsed_ms,
            )
            return True
