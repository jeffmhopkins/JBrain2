"""Keep a shared `--kv-unified` pool from running out under a pinned request.

When the pool runs dry mid-decode, llama-server first purges idle slots in ITS order, then
halves its batch, and if that still fails answers HTTP 500 "Context size has been exceeded."
to EVERY busy slot and clears their KV (FLASH_NEXT_ENGINE_PLAN §4a). One oversized research
turn would kill the owner's chat mid-answer. So before a pinned request is sent this projects
what the pool will hold once it runs, and frees idle slots in `KvPool.eviction_order` — the
cheapest prefix first, jerv's last — until the projection fits. Busy slots are never chosen,
and every erase is preceded by a fresh `/slots` read that must still show its slot idle: an
erase that reaches a busy slot is deferred by the server until that slot finishes, and the
guard abandons it after `ERASE_TIMEOUT_S`. An abandoned erase may still land later — whether
the client's cancel drops it server-side is unverified — wiping that role's prefix after its
turn. The fresh read is what keeps that rare; it costs a re-prefill, never a broken answer.

`/slots` is the shared truth between the api and worker processes, each with its own guard. A
guard also remembers what it has itself just placed (`_pending`): a request is not
`is_processing` until llama-server picks it up, and two calls placed back to back would
otherwise each see the other's slot as empty. The OTHER process's placements are not in it:
between that process's read and the moment llama-server starts its request, the slot still
reads idle and empty here. That window (one HTTP round trip) is the residual race this wave
accepts; a busy slot still prefilling is charged its whole cap to cover the rest of it —
unless this process placed a call there, whose size it knows.

The same `/slots` read is the layout check (`slot_roles.layout_matches`). A server still on a
pre-pool config would WRAP an `id_slot` past its count onto the wrong slot with no error, so
on a mismatch the call goes unpinned — its cap still applies. An UNREADABLE `/slots` (a slow or
failed read) is not a mismatch: if this model's layout matched within `LAYOUT_TTL_S` the call
is still pinned, and only the eviction projection is skipped.
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

from jbrain.llm import local_catalog
from jbrain.llm.errors import LlmTransientError
from jbrain.llm.prefill import SlotsReader
from jbrain.llm.slot_roles import (
    KvPool,
    SlotAdmission,
    SlotCapError,
    SlotRole,
    admit,
    layout_matches,
)

log = structlog.get_logger()

# How long a read of the saved pool size is trusted (`KvPoolGuard._sized`). A resize also
# unloads the model, so the window where a stale size could matter is a reload, not a call.
POOL_SIZE_TTL_S: Final = 10.0

# Erase one slot of a served model. False means the server cannot erase at all (501: it was
# started without `--slot-save-path`, i.e. a stale config); any other failure raises.
SlotEraser = Callable[[str, int], Awaitable[bool]]

# How long a call may wait for busy slots to finish before it is deferred. Long enough for a
# typical background turn to end, short against the job queue's own retry backoff.
DEFAULT_WAIT_S: Final = 120.0
# The owner's chat turn: he is watching a spinner, so it says "busy" fast rather than sit two
# minutes behind a research run.
INTERACTIVE_WAIT_S: Final = 15.0
DEFAULT_POLL_S: Final = 2.0
# A `/slots` read that takes longer than this is treated as unreadable. The read sits in front
# of every pinned call, and the gateway client's own timeout is far longer.
READ_TIMEOUT_S: Final = 4.0
# How long a matching layout is trusted when `/slots` cannot be read. Short: the other process
# may re-stamp and reload the model, and nothing here hears about it.
LAYOUT_TTL_S: Final = 60.0
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


def _prefilling(slot: Mapping[str, object]) -> bool:
    return _busy(slot) and _int(_next_token(slot).get("n_decoded")) == 0


def _held(slot: Mapping[str, object]) -> int:
    # The slot's whole cached sequence on its own: llama-server pushes each sampled token into
    # the slot's prompt tokens as it decodes (server-context.cpp ~510 at 869034b).
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
    if _prefilling(slot):
        return max(_held(slot) + _int(remain), cap)
    if not isinstance(remain, int) or remain < 0:
        return cap
    return min(_held(slot) + remain, cap)


def _charge(slot: Mapping[str, object], cap: int, pending: int) -> int:
    """What a slot is charged against the pool, given this process's pending figure on it."""
    if pending and _prefilling(slot):
        # Most likely our own call being prefilled, whose real size (prompt + output budget)
        # is known here — the whole cap would overstate it, and could refuse a call that fits.
        return max(_held(slot), pending)
    return max(_projected(slot, cap), pending)


def _by_id(slots: Sequence[Mapping[str, object]]) -> dict[int, Mapping[str, object]]:
    return {i: s for i, s in ((s.get("id"), s) for s in slots) if isinstance(i, int)}


def projected_cells(
    pool: KvPool, slots: Sequence[Mapping[str, object]], *, exclude: int | None = None
) -> int:
    """Cells the pool will hold at most, off one `/slots` read, leaving out slot `exclude`.

    For a writer outside the router — the disk prefix store restoring a file into an idle slot
    — that must not push the pool past `n_ctx` under a busy call, which llama-server answers by
    failing every busy request. Same charging rule as the guard's own projection."""
    live = _by_id(slots)
    return sum(
        _projected(live.get(r.slot, {}), r.cap_tokens)
        for r in pool.reservations
        if r.slot != exclude
    )


class KvPoolGuard:
    def __init__(
        self,
        read: SlotsReader,
        erase: SlotEraser,
        *,
        wait_s: float = DEFAULT_WAIT_S,
        poll_s: float = DEFAULT_POLL_S,
        read_timeout_s: float = READ_TIMEOUT_S,
        erase_timeout_s: float = ERASE_TIMEOUT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        windows_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
    ) -> None:
        self._read = read
        # The saved per-model overrides, where a pooled model's pool size lives
        # (`local_catalog.effective_pool`). Without it the guard holds calls to the catalog
        # default, which on a box serving a larger pool only frees idle slots sooner.
        self._windows_loader = windows_loader
        # (read at, overrides) — the size changes only on an owner/debug PUT, so a short TTL
        # spares a settings read on every pinned call.
        self._windows_cache: tuple[float, Mapping[str, int]] | None = None
        self._erase = erase
        self._wait_s = wait_s
        self._poll_s = poll_s
        self._read_timeout_s = read_timeout_s
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
        # Model -> when a `/slots` read last matched its pool's layout.
        self._layout_seen: dict[str, float] = {}
        # (served model, slot) -> tokens the disk prefix store restored there and no request
        # has used yet. Such a slot's `/slots` entry carries no `n_prompt_tokens` when it never
        # ran a request, so without this it would read as free while its cells are taken.
        self._restored: dict[tuple[str, int], int] = {}
        # Told of each slot this guard erases (the disk store drops its memo for that slot).
        self._erase_listeners: list[Callable[[str, int], None]] = []
        # Which chat pair slot holds the most recent conversation (the disk store knows), so
        # it is freed last whatever its static rank. None: the pool's static order.
        self._keep_last: Callable[[str], int | None] | None = None

    async def _slots(self, model: str) -> list[dict[str, object]] | None:
        try:
            async with asyncio.timeout(self._read_timeout_s):
                return await self._read(model)
        except Exception:  # noqa: BLE001 — an unreadable /slots degrades, never fails the call
            log.warning("llm.slot_read_failed", model=model, exc_info=True)
            return None

    async def _layout(self, model: str, pool: KvPool) -> list[dict[str, object]] | None:
        """A fresh read when it is this pool's layout, else None (unreadable or mismatched)."""
        read = await self._slots(model)
        if read is None:
            return None
        if layout_matches(pool, read):
            self._layout_seen[model] = self._clock()
            return read
        self._layout_seen.pop(model, None)
        return None

    async def _sized(self, model: str, pool: KvPool) -> KvPool:
        """`pool` at the size `model` is actually served with. A failed read keeps the catalog
        default: the smaller pool, so the guard errs toward evicting early, never overrunning."""
        if self._windows_loader is None or not pool.cell_choices:
            return pool
        model_id = local_catalog.id_for_served(model)
        if model_id is None:
            return pool
        cached = self._windows_cache
        if cached is not None and self._clock() - cached[0] < POOL_SIZE_TTL_S:
            return pool.resized(cached[1].get(model_id))
        try:
            windows = await self._windows_loader()
        except Exception:  # noqa: BLE001 — a settings hiccup must not fail the call
            log.warning("llm.pool_size_unread", model=model, exc_info=True)
            return pool
        self._windows_cache = (self._clock(), dict(windows))
        return pool.resized(windows.get(model_id))

    def _layout_recent(self, model: str) -> bool:
        seen = self._layout_seen.get(model)
        return seen is not None and self._clock() - seen < LAYOUT_TTL_S

    @contextlib.asynccontextmanager
    async def placed(
        self,
        model: str,
        pool: KvPool,
        role: SlotRole,
        *,
        prompt_tokens: int,
        max_tokens: int,
        wait_s: float | None = None,
    ) -> AsyncIterator[Placement]:
        """Admit a call against its slot's cap, make room for it in the pool, and hold its
        projected cells as pending for as long as the caller is inside the block.

        Raises `SlotCapError` (permanent) before anything else when the call cannot fit its
        cap, and `KvPoolBusyError` (transient) when busy slots hold the room it needs for
        longer than `wait_s` (the guard's default when None)."""
        pool = await self._sized(model, pool)
        admission = admit(pool, role, prompt_tokens=prompt_tokens, max_tokens=max_tokens)
        read = await self._slots(model)
        ticket = next(self._tickets)
        if read is not None and layout_matches(pool, read):
            self._layout_seen[model] = self._clock()
            admission = await self._make_room(
                model,
                pool,
                admission,
                prompt_tokens,
                max_tokens,
                read,
                ticket,
                self._wait_s if wait_s is None else wait_s,
            )
        elif read is None and self._layout_recent(model):
            # The layout matched moments ago, so the slot id is safe to send; only the
            # projection needs a read. The call is still recorded for the next decision.
            log.info("llm.slot_pinned_unread", model=model, role=str(admission.role))
            self._hold(model, admission.slot, ticket, prompt_tokens + admission.max_tokens)
        else:
            if read is not None:
                self._layout_seen.pop(model, None)
                log.warning(
                    "llm.slot_layout_mismatch",
                    model=model,
                    live_slots=len(read),
                    pool_slots=pool.n_slots,
                )
            self._log_clamp(model, admission, prompt_tokens, max_tokens)
            yield Placement(None, admission.role, admission.max_tokens, prompt_tokens)
            return
        self._log_clamp(model, admission, prompt_tokens, max_tokens)
        key = (model, admission.slot)
        try:
            yield Placement(admission.slot, admission.role, admission.max_tokens, prompt_tokens)
        finally:
            held = self._pending.get(key, {})
            held.pop(ticket, None)
            if not held:
                self._pending.pop(key, None)

    @staticmethod
    def _log_clamp(model: str, admission: SlotAdmission, prompt_tokens: int, asked: int) -> None:
        if admission.clamped:
            log.info(
                "llm.slot_output_clamped",
                model=model,
                role=str(admission.role),
                prompt_tokens=prompt_tokens,
                asked=asked,
                sent=admission.max_tokens,
            )

    def _hold(self, model: str, slot: int, ticket: int, cells: int) -> None:
        self._pending.setdefault((model, slot), {})[ticket] = cells

    def _pending_on(self, model: str, slot: int) -> int:
        return max(self._pending.get((model, slot), {}).values(), default=0)

    def note_restored(self, model: str, slot: int, tokens: int) -> None:
        """A disk restore put `tokens` cells into `slot`; charge them until a read shows the
        slot's own size or this guard erases it."""
        self._restored[(model, slot)] = tokens

    def forget_restored(self, model: str) -> None:
        """The model was unloaded or reloaded: every restored slot of it is gone."""
        for key in [k for k in self._restored if k[0] == model]:
            del self._restored[key]

    def add_erase_listener(self, listener: Callable[[str, int], None]) -> None:
        self._erase_listeners.append(listener)

    def set_keep_last(self, source: Callable[[str], int | None]) -> None:
        self._keep_last = source

    def _eviction_order(
        self, model: str, pool: KvPool, slots: Sequence[Mapping[str, object]] = ()
    ) -> list[int]:
        keep: int | None = None
        if self._keep_last is not None:
            with contextlib.suppress(Exception):  # a ranking hint, never a failed call
                keep = self._keep_last(model)
        if keep is None and pool.chat_pair is not None:
            # No store says which chat slot holds the latest conversation (the worker's guard
            # has none, and an api restart forgets). The larger cache is the better guess: the
            # warm one holds only the jerv prefix, a conversation grows past it — so the
            # smaller goes first. Static rank alone would free slot 0 even while it held the
            # latest chat.
            live = _by_id(slots)
            sizes = {pool.slot(r): _held(live.get(pool.slot(r), {})) for r in pool.chat_pair}
            if len(set(sizes.values())) > 1:
                keep = max(sizes, key=lambda s: sizes[s])
        return pool.eviction_order(keep_last=keep)

    async def reserve_restore(self, model: str, pool: KvPool, slot: int, need: int) -> int | None:
        """`fits`, and when it does, hold `need` cells on `slot` as pending in the SAME locked
        decision — so a placement deciding while the multi-second restore streams already
        counts it. Returns the ticket to pass to `end_restore`, or None when it does not fit
        (nothing held)."""
        pool = await self._sized(model, pool)
        async with self._lock:
            read = await self._layout(model, pool)
            if read is None:
                return None
            total, _freeable = self._occupancy(model, pool, read, slot, need)
            if total > pool.n_ctx:
                return None
            ticket = next(self._tickets)
            self._hold(model, slot, ticket, need)
            return ticket

    def end_restore(self, model: str, slot: int, ticket: int) -> None:
        """Release a restore's pending hold. A successful restore has called `note_restored`
        first, so the slot stays charged across the hand-over; a failed one frees the cells."""
        held = self._pending.get((model, slot), {})
        held.pop(ticket, None)
        if not held:
            self._pending.pop((model, slot), None)

    async def erase_for_restore(self, model: str, pool: KvPool, slot: int, ticket: int) -> bool:
        """Erase `slot` so the disk store can restore over what it holds, under the decision
        lock and off a fresh `/slots` read that must still show it idle with no call but the
        restore's own (`ticket`) placed on it — the same discipline as an eviction's erase, so
        a turn that started on the slot meanwhile is never wiped. Tells the erase listeners."""
        async with self._lock:
            read = await self._layout(model, pool)
            if read is None:
                return False
            others = {t for t in self._pending.get((model, slot), {}) if t != ticket}
            if _busy(_by_id(read).get(slot, {})) or others:
                return False
            erased = await self._erase_one(model, slot)
            if erased is False:
                self._no_erase[model] = self._clock()
            if not erased:
                return False
            self._restored.pop((model, slot), None)
            for listener in self._erase_listeners:
                listener(model, slot)
            return True

    async def fits(self, model: str, pool: KvPool, slot: int, need: int) -> bool:
        """Whether writing `need` cells into idle `slot` keeps the pool within its size, judged
        like a placement: a fresh `/slots` read under the decision lock, this process's pending
        calls included. A writer outside the router — the disk prefix store's restores — asks
        here so it can never push the pool past `n_ctx` under a busy call. An unreadable or
        mismatched layout fits nothing."""
        pool = await self._sized(model, pool)
        async with self._lock:
            read = await self._layout(model, pool)
            if read is None:
                return False
            total, _freeable = self._occupancy(model, pool, read, slot, need)
            return total <= pool.n_ctx

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
            if "n_prompt_tokens" in slot:
                self._restored.pop((model, r.slot), None)
            restored = self._restored.get((model, r.slot), 0)
            if r.slot == target:
                # This call replaces the slot's contents, but a busy occupant finishes first.
                current = _charge(slot, r.cap_tokens, pending) if _busy(slot) else pending
                total += max(current, need)
                continue
            # A restored-unused slot is charged its restored size, and stays erasable.
            projected = max(_charge(slot, r.cap_tokens, pending), restored)
            total += projected
            if not _busy(slot) and pending == 0 and projected > 0:
                freeable[r.slot] = projected
        return total, freeable

    def _overflow(
        self,
        model: str,
        pool: KvPool,
        admission: SlotAdmission,
        slots: Sequence[Mapping[str, object]],
        prompt_tokens: int,
        max_tokens: int,
    ) -> SlotAdmission:
        """The call admitted to its role's overflow when its own slot is taken, the overflow
        slot is free, and the call fits the overflow's cap; otherwise unchanged (a busy pinned
        slot just queues). A slot this process has a call placed on counts as taken: it reads
        idle until llama-server picks that call up."""
        target = pool.reservation(admission.role).overflow
        if target is None:
            return admission
        live = _by_id(slots)

        def taken(slot: int) -> bool:
            return _busy(live.get(slot, {})) or self._pending_on(model, slot) > 0

        if not taken(admission.slot) or taken(pool.slot(target)):
            return admission
        try:
            return admit(pool, target, prompt_tokens=prompt_tokens, max_tokens=max_tokens)
        except SlotCapError:
            return admission

    async def _make_room(
        self,
        model: str,
        pool: KvPool,
        admission: SlotAdmission,
        prompt_tokens: int,
        max_tokens: int,
        slots: Sequence[Mapping[str, object]],
        ticket: int,
        wait_s: float,
    ) -> SlotAdmission:
        """Pick the slot (its own or its overflow), wait until the pool can hold the call,
        freeing idle slots as needed, then record it as pending — all inside the lock, so the
        next decision already counts it. Returns the admission actually placed."""
        deadline = self._clock() + wait_s
        # The read `placed` just made is reused when nothing else was deciding; after waiting
        # for the lock it may be stale (another call erased or placed), so it is read again.
        current: Sequence[Mapping[str, object]] | None = None if self._lock.locked() else slots
        chosen = admission
        while True:
            async with self._lock:
                if current is None:
                    current = await self._layout(model, pool) or slots
                chosen = self._overflow(model, pool, admission, current, prompt_tokens, max_tokens)
                need = prompt_tokens + chosen.max_tokens
                if await self._evict_until_fits(model, pool, chosen.slot, need, current, deadline):
                    self._hold(model, chosen.slot, ticket, need)
                    return chosen
            if self._clock() >= deadline:
                raise KvPoolBusyError(
                    f"the {chosen.role} slot needs ~{prompt_tokens + chosen.max_tokens} cells "
                    f"of the {pool.n_ctx}-cell pool, and busy slots hold them"
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
            # Abandoned here, possibly still queued server-side (module docstring).
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
        """Erase idle slots in eviction order until the projection fits.

        Each erase is preceded by its own `/slots` read, and goes ahead only if that read
        still shows the slot idle with nothing of ours placed on it — the read that chose it
        may be seconds old, and a slot the other process just started on must not be wiped.
        That includes jerv's slot, which ranks last: it is only ever erased off a read taken
        immediately before. An unreadable pool erases nothing. After every erase the pool is
        read again: the cells freed are whatever the server now reports, not what was
        promised."""
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
                (
                    s
                    for s in self._eviction_order(model, pool, slots)
                    if s in freeable and s not in tried
                ),
                None,
            )
            if slot is None or self._clock() >= deadline:
                return False
            tried.add(slot)
            fresh = await self._layout(model, pool)
            if fresh is None:
                return False
            slots = fresh
            total, freeable = self._occupancy(model, pool, slots, target, need)
            if total <= pool.n_ctx:
                return True
            if slot not in freeable:
                continue
            erased = await self._erase_one(model, slot)
            if erased is False:
                self._no_erase[model] = self._clock()
                log.warning("llm.slot_erase_unsupported", model=model)
                return True
            if erased is None:
                continue
            self._restored.pop((model, slot), None)
            for listener in self._erase_listeners:
                listener(model, slot)
            log.info(
                "llm.slot_evicted",
                model=model,
                slot=slot,
                role=str(pool.by_slot(slot).role),
                tokens_freed=freeable[slot],
            )
            after = await self._layout(model, pool)
            slots = after if after is not None else [s for s in slots if s.get("id") != slot]
