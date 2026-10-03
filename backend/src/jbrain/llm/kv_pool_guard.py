"""Keep a shared `--kv-unified` pool from running out under a pinned request.

When the pool runs dry mid-decode, llama-server first purges idle slots in ITS order, then
halves its batch, and if that still fails answers HTTP 500 "Context size has been exceeded."
to EVERY busy slot and clears their KV (FLASH_NEXT_ENGINE_PLAN §4a). One oversized research
turn would kill the owner's chat mid-answer. So before a pinned request is sent this projects
what the pool will hold once it runs, and frees idle slots in `KvPool.eviction_order` — the
cheapest prefix first, jerv's last — until the projection fits. Busy slots are never touched:
erasing one is deferred by the server until it finishes, which frees nothing now.

`/slots` is the shared truth between the api and worker processes, each with its own guard. A
guard also remembers what it has itself just placed (`_pending`): a request is not
`is_processing` until llama-server picks it up, and two calls placed back to back would
otherwise each see the other's slot as empty. The OTHER process's placements are not in it:
between that process's read and the moment llama-server starts its request, the slot still
reads idle and empty here. That window (one HTTP round trip) is the residual race this wave
accepts; a busy slot still prefilling is charged its whole cap to cover the rest of it.

The same `/slots` read is the layout check. A server still running a pre-pool config (fewer
slots, until the next re-stamp) would WRAP an `id_slot` past its count onto the wrong slot
with no error, so on any mismatch the call goes unpinned — its cap still applies.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import count
from typing import Final

import structlog

from jbrain.llm.errors import LlmTransientError
from jbrain.llm.prefill import SlotsReader
from jbrain.llm.slot_roles import KvPool, SlotAdmission, SlotCapError, SlotRole, admit

log = structlog.get_logger()

# Erase one slot of a served model. False means the server cannot erase at all (501: it was
# started without `--slot-save-path`, i.e. a stale config); any other failure raises.
SlotEraser = Callable[[str, int], Awaitable[bool]]

# How long a call may wait for busy slots to finish before it is deferred. Long enough for a
# typical background turn to end, short against the job queue's own retry backoff.
DEFAULT_WAIT_S: Final = 120.0
DEFAULT_POLL_S: Final = 2.0
# An erase llama-server defers (the slot turned busy after our read) blocks until that slot
# finishes. It frees nothing now, so it is abandoned quickly and the next candidate tried.
ERASE_TIMEOUT_S: Final = 3.0
# How long a server that answered erase with 501 is trusted to still lack `--slot-save-path`;
# a re-stamped config gains it without this process restarting.
NO_ERASE_TTL_S: Final = 600.0


class KvPoolBusyError(LlmTransientError):
    """The pool cannot hold this call until busy slots finish, and they did not finish in
    time. Transient: the worker retries the job later; the chat reports a busy box."""


@dataclass(frozen=True)
class Placement:
    """Where a call goes: its slot (None = unpinned), role, and the output budget to send."""

    slot: int | None
    role: SlotRole
    max_tokens: int
    prompt_tokens: int


def _next_token(slot: Mapping[str, object]) -> Mapping[str, object]:
    # A list on current builds, a bare object on older ones.
    raw = slot.get("next_token")
    if isinstance(raw, list) and raw and isinstance(raw[0], Mapping):
        return raw[0]
    return raw if isinstance(raw, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _busy(slot: Mapping[str, object]) -> bool:
    return bool(slot.get("is_processing"))


def _held(slot: Mapping[str, object]) -> int:
    # llama-server counts generated tokens into `n_prompt_tokens` as they are decoded, so it is
    # the slot's whole cached sequence on its own.
    return _int(slot.get("n_prompt_tokens"))


def _projected(slot: Mapping[str, object], cap: int) -> int:
    """Cells the slot will hold at most. A busy one grows by what it may still generate,
    bounded by its cap; an unbounded generation (`n_remain` < 0) is charged the whole cap.

    A slot still PREFILLING (busy, nothing decoded) is charged at least its cap: there
    `n_prompt_tokens` grows a batch at a time and says nothing about the prompt's real size."""
    if not _busy(slot):
        return _held(slot)
    token = _next_token(slot)
    remain = token.get("n_remain")
    if _int(token.get("n_decoded")) == 0:
        return max(_held(slot) + _int(remain), cap)
    if not isinstance(remain, int) or remain < 0:
        return cap
    return min(_held(slot) + remain, cap)


def _by_id(slots: Sequence[Mapping[str, object]]) -> dict[int, Mapping[str, object]]:
    return {i: s for i, s in ((s.get("id"), s) for s in slots) if isinstance(i, int)}


class KvPoolGuard:
    def __init__(
        self,
        read: SlotsReader,
        erase: SlotEraser,
        *,
        wait_s: float = DEFAULT_WAIT_S,
        poll_s: float = DEFAULT_POLL_S,
        erase_timeout_s: float = ERASE_TIMEOUT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._erase = erase
        self._wait_s = wait_s
        self._poll_s = poll_s
        self._erase_timeout_s = erase_timeout_s
        self._sleep = sleep
        self._clock = clock
        # One decision at a time per process: two calls deciding together would each count
        # the same idle cells as free.
        self._lock = asyncio.Lock()
        # (served model, slot) -> {ticket: projected cells} for calls placed and not finished.
        self._pending: dict[tuple[str, int], dict[int, int]] = {}
        self._tickets = count()
        # Model -> when its server answered erase with 501; logged once, let through until
        # the entry expires.
        self._no_erase: dict[str, float] = {}

    async def _slots(self, model: str) -> list[dict[str, object]] | None:
        try:
            return await self._read(model)
        except Exception:  # noqa: BLE001 — an unreadable /slots degrades to unpinned
            log.warning("llm.slot_read_failed", model=model, exc_info=True)
            return None

    @contextlib.asynccontextmanager
    async def placed(
        self,
        model: str,
        pool: KvPool,
        role: SlotRole,
        *,
        prompt_tokens: int,
        max_tokens: int,
        allow_clamp: bool = True,
    ) -> AsyncIterator[Placement]:
        """Admit a call against its slot's cap, make room for it in the pool, and hold its
        projected cells as pending for as long as the caller is inside the block.

        Raises `SlotCapError` (permanent) before anything else when the call cannot fit its
        cap, and `KvPoolBusyError` (transient) when busy slots hold the room it needs."""
        read = await self._slots(model)
        slots = read if read is not None and len(read) == pool.n_slots else None
        if slots is not None:
            role = _overflow(pool, role, slots, prompt_tokens, max_tokens)
        admission = admit(
            pool, role, prompt_tokens=prompt_tokens, max_tokens=max_tokens, allow_clamp=allow_clamp
        )
        if admission.clamped:
            log.info(
                "llm.slot_output_clamped",
                model=model,
                role=str(admission.role),
                prompt_tokens=prompt_tokens,
                asked=max_tokens,
                sent=admission.max_tokens,
            )
        if slots is None:
            if read is not None:
                log.warning(
                    "llm.slot_layout_mismatch",
                    model=model,
                    live_slots=len(read),
                    pool_slots=pool.n_slots,
                )
            yield Placement(None, admission.role, admission.max_tokens, prompt_tokens)
            return
        need = prompt_tokens + admission.max_tokens
        key = (model, admission.slot)
        ticket = next(self._tickets)
        await self._make_room(model, pool, admission, need, slots, ticket)
        try:
            yield Placement(admission.slot, admission.role, admission.max_tokens, prompt_tokens)
        finally:
            held = self._pending.get(key, {})
            held.pop(ticket, None)
            if not held:
                self._pending.pop(key, None)

    def _pending_on(self, model: str, slot: int) -> int:
        return max(self._pending.get((model, slot), {}).values(), default=0)

    def _occupancy(
        self,
        model: str,
        pool: KvPool,
        slots: Sequence[Mapping[str, object]],
        target: int,
        need: int,
    ) -> tuple[int, dict[int, int]]:
        """Projected pool cells with this call placed, and what each idle non-target slot
        would free if erased."""
        total = 0
        freeable: dict[int, int] = {}
        live = _by_id(slots)
        for r in pool.reservations:
            slot = live.get(r.slot, {})
            pending = self._pending_on(model, r.slot)
            if r.slot == target:
                # This call replaces the slot's contents, but a busy occupant finishes first.
                current = _projected(slot, r.cap_tokens) if _busy(slot) else 0
                total += max(current, pending, need)
                continue
            projected = max(_projected(slot, r.cap_tokens), pending)
            total += projected
            if not _busy(slot) and pending == 0 and projected > 0:
                freeable[r.slot] = projected
        return total, freeable

    async def _make_room(
        self,
        model: str,
        pool: KvPool,
        admission: SlotAdmission,
        need: int,
        slots: Sequence[Mapping[str, object]],
        ticket: int,
    ) -> None:
        """Wait until the pool can hold this call, freeing idle slots as needed, then record it
        as pending — inside the lock, so the next decision already counts it."""
        deadline = self._clock() + self._wait_s
        # The read `placed` just made is reused when nothing else was deciding; after waiting
        # for the lock it may be stale (another call erased or placed), so it is read again.
        current: Sequence[Mapping[str, object]] | None = None if self._lock.locked() else slots
        while True:
            async with self._lock:
                if current is None:
                    fresh = await self._slots(model)
                    current = fresh if fresh is not None and len(fresh) == pool.n_slots else slots
                if await self._evict_until_fits(
                    model, pool, admission.slot, need, current, deadline
                ):
                    self._pending.setdefault((model, admission.slot), {})[ticket] = need
                    return
            if self._clock() >= deadline:
                raise KvPoolBusyError(
                    f"the {admission.role} slot needs ~{need} cells of the {pool.n_ctx}-cell "
                    "pool, and busy slots hold them"
                )
            await self._sleep(self._poll_s)
            current = None

    def _cannot_erase(self, model: str) -> bool:
        since = self._no_erase.get(model)
        if since is None:
            return False
        if self._clock() - since < NO_ERASE_TTL_S:
            return True
        del self._no_erase[model]
        return False

    async def _erase_one(self, model: str, slot: int) -> bool | None:
        """True erased, False the server cannot erase at all (501), None not freed now."""
        try:
            async with asyncio.timeout(self._erase_timeout_s):
                return await self._erase(model, slot)
        except TimeoutError:
            log.warning("llm.slot_erase_deferred", model=model, slot=slot)
        except Exception:  # noqa: BLE001 — a failed erase just frees nothing
            log.warning("llm.slot_erase_failed", model=model, slot=slot, exc_info=True)
        return None

    async def _evict_until_fits(
        self,
        model: str,
        pool: KvPool,
        target: int,
        need: int,
        slots: Sequence[Mapping[str, object]],
        deadline: float,
    ) -> bool:
        """Erase idle slots in eviction order until the projection fits. After every erase the
        pool is read again: a slot can turn busy between our read and its erase, and the cells
        freed are whatever the server now reports, not what the stale read promised."""
        tried: set[int] = set()
        while True:
            total, freeable = self._occupancy(model, pool, slots, target, need)
            if total <= pool.n_ctx:
                return True
            if self._cannot_erase(model):
                # Without `--slot-save-path` nothing can be erased; the engine purges idle
                # slots itself in its own order, which is worse but not a reason to refuse.
                return True
            slot = next(
                (s for s in pool.eviction_order() if s in freeable and s not in tried), None
            )
            if slot is None or self._clock() >= deadline:
                return False
            tried.add(slot)
            erased = await self._erase_one(model, slot)
            if erased is False:
                self._no_erase[model] = self._clock()
                log.warning("llm.slot_erase_unsupported", model=model)
                return True
            if erased is None:
                continue
            log.info(
                "llm.slot_evicted",
                model=model,
                slot=slot,
                role=str(pool.by_slot(slot).role),
                tokens_freed=freeable[slot],
            )
            fresh = await self._slots(model)
            if fresh is not None and len(fresh) == pool.n_slots:
                slots = fresh
            else:
                slots = [s for s in slots if s.get("id") != slot]


def _overflow(
    pool: KvPool,
    role: SlotRole,
    slots: Sequence[Mapping[str, object]],
    prompt_tokens: int,
    max_tokens: int,
) -> SlotRole:
    """The role's overflow when its own slot is busy, the overflow slot is idle, and the call
    fits the overflow's cap; otherwise the role itself (a busy pinned slot just queues)."""
    target = pool.reservation(role).overflow
    if target is None:
        return role
    live = _by_id(slots)
    if not _busy(live.get(pool.slot(role), {})) or _busy(live.get(pool.slot(target), {})):
        return role
    try:
        admit(pool, target, prompt_tokens=prompt_tokens, max_tokens=max_tokens)
    except SlotCapError:
        return role
    return target
