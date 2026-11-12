"""The creature's system prefix kept on disk, so its first turn after a restart is not cold.

Speed plan Phase 3 (docs/plans/GROWBOT_PLAN.md §2 "Speed work"). The creature's ~7k-token
system message is the only part of a turn the box's hybrid model reuses, and a restart or an
update loses it: the next turn re-reads it whole, ~21 s. The jerv prompt cache
(`jbrain.llm.kv_prefix`) already saves a primed slot to disk and restores it into its role's
slot; this drives it for the GrowBot role.

- After a cold turn (the call log's `is_cold`), once the creature has been quiet a while, the
  lane primes its own slot with the system message and a one-character user turn and saves
  that slot. Only the system message reaches disk: never a frame, a word or a reply.
- Before a turn that follows a long quiet, the lane asks the store to put the prefix back. The
  store restores only into an EMPTY slot, so a live conversation is never overwritten, and only
  a file saved for this exact system text, model and launch line.

Everything is best-effort: the worst case of any failure is the cold turn this replaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Final, Protocol

from jbrain.llm.openai_slot_fit import fit_openai_request, pinned_request
from jbrain.llm.slot_roles import GROWBOT_ROLE

log = logging.getLogger(__name__)

# A turn after this long quiet asks the store whether the slot lost its prefix (a restart, a
# model reload). Closer turns ran on a warm slot moments ago, and each check reads `/slots`.
RESTORE_AFTER_QUIET_S: Final = 300.0
# The bound on a restore check, so a stuck gateway cannot hold a turn.
RESTORE_TIMEOUT_S: Final = 30.0
# A save primes the creature's slot, so it waits until the creature has been quiet this long,
# and gives up after `SAVE_GIVE_UP_S` of chatter (the next cold turn tries again).
SAVE_QUIET_S: Final = 20.0
SAVE_GIVE_UP_S: Final = 600.0
_POLL_S: Final = 5.0
# Shorter than this, a system message is not worth a file (the store's own floor is 4,096
# tokens; ~4 characters a token).
MIN_SYSTEM_CHARS: Final = 16_000


class PrefixStore(Protocol):
    async def restore_if_lost(
        self, served_model: str, system: str, tools: list[Any], *, role: Any = None
    ) -> bool: ...

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: list[Any],
        prime_tokens: int,
        *,
        role: Any = None,
    ) -> bool: ...


async def restore(store: PrefixStore | None, model: str, system: str) -> bool:
    """Put the creature's prefix back into its slot if the slot lost it and a file exists."""
    if store is None or len(system) < MIN_SYSTEM_CHARS:
        return False
    try:
        return await asyncio.wait_for(
            store.restore_if_lost(model, system, [], role=GROWBOT_ROLE), RESTORE_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001 - the turn runs cold rather than fail
        log.warning("growbot prefix restore failed model=%s", model, exc_info=True)
        return False


class PrefixSaver:
    """One save at a time, and each system text saved once per process."""

    def __init__(self) -> None:
        self.saved: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def schedule(
        self,
        *,
        store: PrefixStore | None,
        model: str,
        system: str,
        key: str,
        last_turn: Callable[[], float | None],
        lock: asyncio.Lock | None,
        pool_guard: Any,
        client_factory: Callable[[], Any],
        apply_level: Callable[[dict[str, Any]], None],
    ) -> bool:
        if store is None or len(system) < MIN_SYSTEM_CHARS or key in self.saved or self.busy():
            return False
        self._task = asyncio.create_task(
            self._save(
                store, model, system, key, last_turn, lock, pool_guard, client_factory, apply_level
            )
        )
        return True

    async def _save(
        self,
        store: PrefixStore,
        model: str,
        system: str,
        key: str,
        last_turn: Callable[[], float | None],
        lock: asyncio.Lock | None,
        pool_guard: Any,
        client_factory: Callable[[], Any],
        apply_level: Callable[[dict[str, Any]], None],
    ) -> None:
        try:
            if not await _wait_quiet(last_turn):
                return
            guard = lock if lock is not None else contextlib.nullcontext()
            async with guard:
                # A turn that slipped in while we waited for the lock reset the clock.
                quiet = last_turn()
                if quiet is not None and time.monotonic() - quiet < SAVE_QUIET_S:
                    return
                tokens = await _prime(model, system, pool_guard, client_factory, apply_level)
                if tokens and await store.save_after_prime(
                    model, system, [], tokens, role=GROWBOT_ROLE
                ):
                    self.saved.add(key)
                    log.info("growbot prefix saved model=%s tokens=%s", model, tokens)
        except Exception:  # noqa: BLE001 - a lost save costs a future cold turn, nothing now
            log.warning("growbot prefix save failed model=%s", model, exc_info=True)


async def _wait_quiet(last_turn: Callable[[], float | None]) -> bool:
    deadline = time.monotonic() + SAVE_GIVE_UP_S
    while time.monotonic() < deadline:
        last = last_turn()
        if last is None or time.monotonic() - last >= SAVE_QUIET_S:
            return True
        await asyncio.sleep(_POLL_S)
    return False


async def _prime(
    model: str,
    system: str,
    pool_guard: Any,
    client_factory: Callable[[], Any],
    apply_level: Callable[[dict[str, Any]], None],
) -> int:
    """Run the system message and a one-character turn through the creature's slot and return
    the prompt's token count, which is what the store matches the slot by. One output token:
    the server keeps no stop token in the cache, so the slot then holds exactly the prompt."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": "."}],
        "max_tokens": 1,
        "stream": False,
    }
    apply_level(body)
    tokens = fit_openai_request(model, body, GROWBOT_ROLE)
    client = client_factory()
    raw = b""
    try:
        async with (
            pinned_request(pool_guard, model, body, tokens, GROWBOT_ROLE),
            client.stream("POST", "/chat/completions", json=body) as upstream,
        ):
            if getattr(upstream, "status_code", 200) >= 400:
                return 0
            async for chunk in upstream.aiter_raw():
                raw += chunk
    finally:
        await client.aclose()
    try:
        usage = json.loads(raw).get("usage") or {}
    except (ValueError, AttributeError):
        return 0
    value = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    return value if type(value) is int else 0
