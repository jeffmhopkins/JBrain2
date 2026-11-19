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

- His whole conversation too (owner, 2026-11-19: "when that slot has sat there inactive ...
  disk cache that KV, and just hard load it from the hard drive next time"): every turn claims
  the slot for his conversation, and the store's keeper saves it once it has been quiet
  `kv_prefix.CONVERSATION_IDLE_SAVE_S`, as it does jerv's chats. A turn after a long quiet
  loads it back when the slot has lost it, so a restart costs a disk read rather than a re-read
  of the day's talk and looks (growbot-looks.js). That file does hold his words and pictures,
  on the box's own disk beside the slot it came from, under the owner's conversation-cache
  toggle (Ops) like jerv's chats; a dream starts a new history, which the next save replaces.
  Never while brain access is on for him: his notes lookups (every domain) then reach his
  prompt, and a file sits outside Postgres, where the domain firewalls cannot reach
  (`api/growbot_llm.py` `_kept_off_disk`).

Everything is best-effort: the worst case of any failure is the cold turn this replaces.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Final, Protocol

from jbrain import model_calls
from jbrain.llm import local_catalog
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
        self,
        served_model: str,
        system: str,
        tools: list[Any],
        *,
        reasoning_effort: str | None = None,
        role: Any = None,
    ) -> bool: ...

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: list[Any],
        prime_tokens: int,
        *,
        reasoning_effort: str | None = None,
        role: Any = None,
    ) -> bool: ...

    def identity_of(
        self, served_model: str, system: str, tools: list[Any], reasoning_effort: str | None
    ) -> str | None: ...

    def note_prefix_used(
        self, served_model: str, fingerprint: str | None, *, role: Any = None
    ) -> None: ...

    async def prepare_conversation(
        self,
        served_model: str,
        conversation_key: str | None,
        system: str,
        tools: list[Any],
        reasoning_effort: str | None,
        *,
        role: Any = None,
        keep_bigger: bool = False,
    ) -> tuple[bool, int]: ...

    def note_conversation_turn(
        self,
        served_model: str,
        conversation_key: str | None,
        *,
        fingerprint: str | None,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        seq: int | None = None,
        role: Any = None,
    ) -> None: ...

    def note_conversation_abandoned(
        self, served_model: str, chat_key: str | None = None, *, role: Any = None
    ) -> None: ...

    async def forget_conversation(self, conversation_key: str) -> int: ...


def _pooled(model: str) -> bool:
    """Only a model with a role pool gives the creature a slot of its own. Unpooled, the store
    keys its restore memo and its slot pick by model alone, so a creature restore would land in
    any idle slot and spend jerv's memo."""
    return local_catalog.pool_of(model) is not None


async def restore(store: PrefixStore | None, model: str, system: str, level: str) -> bool:
    """Put the creature's prefix back into its slot if the slot lost it and a file exists."""
    if store is None or len(system) < MIN_SYSTEM_CHARS or not _pooled(model):
        return False
    try:
        return await asyncio.wait_for(
            store.restore_if_lost(model, system, [], reasoning_effort=level, role=GROWBOT_ROLE),
            RESTORE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - the turn runs cold rather than fail
        log.warning("growbot prefix restore failed model=%s", model, exc_info=True)
        return False


def conversation_key(system: str) -> str:
    """His one conversation, named by the system text it runs under: another persona, or a
    changed prompt, is another conversation, whose file the store keeps apart."""
    return "growbot:" + hashlib.sha256(system.encode()).hexdigest()


async def restore_conversation(
    store: PrefixStore | None, model: str, system: str, level: str
) -> tuple[bool, int | None]:
    """Load his saved conversation back into his slot if the slot lost it. Returns (restored,
    the store's sequence for this turn's note)."""
    if store is None or len(system) < MIN_SYSTEM_CHARS or not _pooled(model):
        return False, None
    try:
        return await asyncio.wait_for(
            store.prepare_conversation(
                model,
                conversation_key(system),
                system,
                [],
                level,
                role=GROWBOT_ROLE,
                keep_bigger=True,
            ),
            RESTORE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - the turn re-reads rather than fail
        log.warning("growbot conversation restore failed model=%s", model, exc_info=True)
        return False, None


def note_turn(
    store: PrefixStore | None,
    model: str,
    system: str,
    level: str,
    *,
    ok: bool,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None,
    seq: int | None,
) -> None:
    """A turn ran in his slot: it now holds his conversation, unsaved (or, for a turn that
    did not finish, something unknown that must never be saved under his name)."""
    if store is None or len(system) < MIN_SYSTEM_CHARS or not _pooled(model):
        return
    try:
        if not ok or not prompt_tokens:
            store.note_conversation_abandoned(model, role=GROWBOT_ROLE)
            return
        store.note_conversation_turn(
            model,
            conversation_key(system),
            fingerprint=store.identity_of(model, system, [], level),
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens or 0,
            cached_tokens=cached_tokens or 0,
            seq=seq,
            role=GROWBOT_ROLE,
        )
    except Exception:  # noqa: BLE001 - bookkeeping; the turn already ran
        log.warning("growbot conversation note failed model=%s", model, exc_info=True)


async def forget(store: PrefixStore | None, system: str) -> None:
    """Delete his conversation's files and keep it off disk until the api restarts: the
    owner's notes can now reach his prompt (brain access, `growbot_brain.py`, every domain),
    and a conversation file sits outside Postgres, where the domain firewalls cannot reach.
    Kept off for the process's life, not just while access is on, because his history goes on
    carrying what a lookup told him after access is turned off."""
    if store is None or len(system) < MIN_SYSTEM_CHARS:
        return
    try:
        await store.forget_conversation(conversation_key(system))
    except Exception:  # noqa: BLE001 - the next brain-on turn tries again
        log.warning("growbot conversation forget failed", exc_info=True)


def consumed(store: PrefixStore | None, model: str, system: str, level: str) -> None:
    """A turn has run on the restored prefix. Left in place, the store's restored-but-unused
    memo would refuse every later restore for this role until the process restarts, so a model
    reload would cost the cold turn this exists to remove."""
    if store is None:
        return
    try:
        fingerprint = store.identity_of(model, system, [], level)
        store.note_prefix_used(model, fingerprint, role=GROWBOT_ROLE)
    except Exception:  # noqa: BLE001 - the memo is bookkeeping; the turn already ran
        log.warning("growbot prefix memo not cleared model=%s", model, exc_info=True)


class PrefixSaver:
    """One save at a time, and each system text tried once per process: a save the store
    declines (no patch, slot not identified) would decline again, and each try primes the slot."""

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
        level: str,
        key: str,
        last_turn: Callable[[], float | None],
        lock: asyncio.Lock | None,
        pool_guard: Any,
        client_factory: Callable[[], Any],
        apply_level: Callable[[dict[str, Any]], None],
    ) -> bool:
        if (
            store is None
            or len(system) < MIN_SYSTEM_CHARS
            or key in self.saved
            or self.busy()
            or not _pooled(model)
        ):
            return False
        self._task = asyncio.create_task(
            self._save(
                store,
                model,
                system,
                level,
                key,
                last_turn,
                lock,
                pool_guard,
                client_factory,
                apply_level,
            )
        )
        return True

    async def _save(
        self,
        store: PrefixStore,
        model: str,
        system: str,
        level: str,
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
                if not tokens:
                    return
                self.saved.add(key)
                if await store.save_after_prime(
                    model, system, [], tokens, reasoning_effort=level, role=GROWBOT_ROLE
                ):
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
            # A full prefill of the creature's prompt, so it is listed beside its thinks.
            model_calls.track(model_calls.CREATURE, "saving its prompt", model=model) as call,
            client.stream("POST", "/chat/completions", json=body) as upstream,
        ):
            if getattr(upstream, "status_code", 200) >= 400:
                call.failed(f"gateway HTTP {upstream.status_code}")
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
