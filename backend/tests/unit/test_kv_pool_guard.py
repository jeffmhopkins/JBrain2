"""The pool guard: what it projects from `/slots`, which slots it frees and in what order,
when it waits, and how it degrades (a stale layout, an unreadable `/slots`, a server that
cannot erase). Faked `/slots` bodies in the shape llama-server sends (see test_prefill.py)."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import httpx
import pytest

from jbrain.llm.kv_pool_guard import LAYOUT_TTL_S, NO_ERASE_TTL_S, KvPoolBusyError, KvPoolGuard
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, SlotCapError, SlotRole

# The guard's arithmetic is pinned to a fixed 1M pool so these numbers don't move when the
# shipped pool is resized.
POOL = dataclasses.replace(FLASH_NEXT_POOL, n_ctx=1_048_576)
MODEL = "qwen3.8-flash-next"


def _slot(
    sid: int, held: int = 0, *, busy: bool = False, remain: int = 0, decoded: int = 0
) -> dict[str, object]:
    return {
        "id": sid,
        "n_ctx": 262_144,
        "is_processing": busy,
        "n_prompt_tokens": held,
        "next_token": [{"n_remain": remain, "n_decoded": decoded}],
    }


def _layout(**held: dict[str, object]) -> list[dict[str, object]]:
    """The pool's slots, empty unless named (`s3=_slot(3, ...)`)."""
    return [held.get(f"s{i}", _slot(i)) for i in range(POOL.n_slots)]


class _Gateway:
    """Scripted `/slots` reads (the last repeats); a slot it erased reads empty afterwards.
    `refuse` names slots whose erase raises, `stall` slots whose erase never returns (a slot
    that turned busy, where llama-server defers the erase)."""

    def __init__(
        self,
        *reads: list[dict[str, object]],
        erase_ok: bool = True,
        refuse: frozenset[int] = frozenset(),
        stall: frozenset[int] = frozenset(),
    ) -> None:
        self._reads = list(reads)
        self.reads = 0
        self.erased: list[int] = []
        self.cleared: set[int] = set()
        self._erase_ok = erase_ok
        self._refuse = refuse
        self._stall = stall

    async def read(self, model: str) -> list[dict[str, object]]:
        assert model == MODEL
        body = self._reads[min(self.reads, len(self._reads) - 1)]
        self.reads += 1
        return [_slot(s["id"]) if s["id"] in self.cleared else s for s in body]  # type: ignore[arg-type]

    async def erase(self, model: str, slot: int) -> bool:
        self.erased.append(slot)
        if slot in self._refuse:
            raise LocalGatewayError("boom")
        if slot in self._stall:
            await asyncio.Event().wait()
        if self._erase_ok:
            self.cleared.add(slot)
        return self._erase_ok


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _guard(gw: _Gateway, *, wait_s: float = 10.0, clock: _Clock | None = None) -> KvPoolGuard:
    clock = clock or _Clock()
    return KvPoolGuard(
        gw.read,
        gw.erase,
        wait_s=wait_s,
        poll_s=2.0,
        erase_timeout_s=0.05,
        sleep=clock.sleep,
        clock=clock,
    )


async def test_a_call_that_fits_is_pinned_to_its_roles_slot_and_erases_nothing() -> None:
    gw = _Gateway(_layout(s0=_slot(0, 30_000)))
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.RESEARCH, prompt_tokens=10_000, max_tokens=4_000
    ) as placed:
        assert placed.slot == POOL.slot(SlotRole.RESEARCH)
        assert placed.max_tokens == 4_000
    assert gw.erased == []


async def test_idle_slots_are_freed_in_eviction_order_never_the_target_or_a_busy_one() -> None:
    # 980k held elsewhere + 200k for this call overruns the 1,048,576-cell pool by ~131k.
    # SMALL (7) goes first, PET (6) is busy and skipped, WORKSHOP (5) is next and that fits.
    gw = _Gateway(
        _layout(
            s0=_slot(0, 250_000),  # the target's own cached prefix: replaced, never erased
            s1=_slot(1, 100_000),
            s2=_slot(2, 250_000),
            s3=_slot(3, 200_000),
            s4=_slot(4, 250_000),
            s5=_slot(5, 100_000),
            s6=_slot(6, 30_000, busy=True),
            s7=_slot(7, 50_000),
        )
    )
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=190_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == 0
    assert gw.erased == [7, 5]


def _busy_pool() -> list[dict[str, object]]:
    # Busy slots 1-5 project 130k + 3 x 255k + 130k = 1,025k of the 1,048,576-cell pool.
    held = {1: 125_000, 2: 250_000, 3: 250_000, 4: 250_000, 5: 125_000}
    return _layout(**{f"s{i}": _slot(i, n, busy=True, remain=5_000) for i, n in held.items()})


async def test_busy_slots_holding_the_pool_make_the_call_wait_then_fail_transiently() -> None:
    gw = _Gateway(_busy_pool())
    with pytest.raises(KvPoolBusyError):
        async with _guard(gw, wait_s=10.0).placed(
            MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=50_000, max_tokens=10_000
        ):
            pytest.fail("must not be placed")
    assert gw.erased == []
    assert gw.reads > 2  # it polled while it waited


async def test_a_busy_slot_that_finishes_inside_the_wait_lets_the_call_through() -> None:
    settled = _layout(**{f"s{i}": _slot(i, 20_000) for i in range(1, 6)})
    gw = _Gateway(_busy_pool(), _busy_pool(), _busy_pool(), settled)
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=50_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == 0
    assert gw.reads == 4 and gw.erased == []


async def test_an_unbounded_busy_generation_is_charged_its_whole_cap() -> None:
    # n_remain -1 (no output budget): each slot may grow to its 256k cap, and four of them
    # are the whole pool however little they hold now.
    busy = {i: _slot(i, 1_000, busy=True, remain=-1) for i in (0, 2, 3, 4)}
    gw = _Gateway(_layout(**{f"s{i}": s for i, s in busy.items()}))
    with pytest.raises(KvPoolBusyError):
        async with _guard(gw, wait_s=0.0).placed(
            MODEL, POOL, SlotRole.WORKSHOP, prompt_tokens=1_000, max_tokens=1_000
        ):
            pass


async def test_a_server_that_cannot_erase_is_logged_once_and_let_through() -> None:
    crowded = _layout(**{f"s{i}": _slot(i, 200_000) for i in range(1, 8)})
    gw = _Gateway(crowded, erase_ok=False)
    guard = _guard(gw)
    for _ in range(2):
        async with guard.placed(
            MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=100_000, max_tokens=10_000
        ) as placed:
            assert placed.slot == 0
    assert gw.erased == [7]  # tried once; the second call does not ask again


async def test_a_live_layout_that_does_not_match_the_pool_goes_unpinned() -> None:
    # A pre-pool config still serving four slots would wrap id_slot 4..7 onto the wrong slot.
    gw = _Gateway([_slot(i) for i in range(4)])
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.PET, prompt_tokens=1_000, max_tokens=500
    ) as placed:
        assert placed.slot is None
    assert gw.erased == []


async def test_the_cap_still_holds_when_the_layout_does_not() -> None:
    gw = _Gateway([_slot(i) for i in range(4)])
    with pytest.raises(SlotCapError):
        async with _guard(gw).placed(
            MODEL, POOL, SlotRole.PET, prompt_tokens=40_000, max_tokens=500
        ):
            pass


async def test_an_unreadable_slots_endpoint_degrades_to_unpinned() -> None:
    async def boom(model: str) -> list[dict[str, object]]:
        raise LocalGatewayError("down")

    async def never(model: str, slot: int) -> bool:
        raise AssertionError("nothing to erase")

    async with KvPoolGuard(boom, never).placed(
        MODEL, POOL, SlotRole.SMALL, prompt_tokens=100, max_tokens=100
    ) as placed:
        assert placed.slot is None


async def test_output_is_clamped_to_what_the_slot_has_left() -> None:
    gw = _Gateway(_layout())
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.INGEST, prompt_tokens=120_000, max_tokens=20_000
    ) as placed:
        assert placed.max_tokens == POOL.cap(SlotRole.INGEST) - 120_000


async def test_the_pet_overflows_to_the_small_slot_when_its_own_is_busy() -> None:
    gw = _Gateway(_layout(s6=_slot(6, 5_000, busy=True, remain=100)))
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.PET, prompt_tokens=2_000, max_tokens=500
    ) as placed:
        assert placed.slot == POOL.slot(SlotRole.SMALL)
        assert placed.role is SlotRole.SMALL


async def test_the_pet_stays_put_when_small_is_busy_too_or_its_own_slot_is_idle() -> None:
    both = _Gateway(
        _layout(s6=_slot(6, 5_000, busy=True), s7=_slot(7, 5_000, busy=True, remain=100))
    )
    async with _guard(both).placed(
        MODEL, POOL, SlotRole.PET, prompt_tokens=2_000, max_tokens=500
    ) as placed:
        assert placed.slot == POOL.slot(SlotRole.PET)
    idle = _Gateway(_layout())
    async with _guard(idle).placed(
        MODEL, POOL, SlotRole.PET, prompt_tokens=2_000, max_tokens=500
    ) as placed:
        assert placed.slot == POOL.slot(SlotRole.PET)


async def test_calls_placed_but_not_yet_running_count_against_the_pool() -> None:
    # Placed calls llama-server has not picked up yet read as idle, empty slots; without the
    # guard's own record each new call would see an empty pool.
    gw = _Gateway(_layout())
    guard = _guard(gw, wait_s=0.0)
    big: dict[str, Any] = {"prompt_tokens": 252_000, "max_tokens": 10_000}
    async with (
        guard.placed(MODEL, POOL, SlotRole.RESEARCH, **big),
        guard.placed(MODEL, POOL, SlotRole.JCODE, **big),
        guard.placed(MODEL, POOL, SlotRole.SCHEDULED, **big),
        guard.placed(MODEL, POOL, SlotRole.INTERACTIVE, **big),
    ):
        with pytest.raises(KvPoolBusyError):
            async with guard.placed(
                MODEL, POOL, SlotRole.SMALL, prompt_tokens=50_000, max_tokens=10_000
            ):
                pass
    # Released on the way out: the same call fits an empty pool.
    async with guard.placed(
        MODEL, POOL, SlotRole.SMALL, prompt_tokens=50_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == POOL.slot(SlotRole.SMALL)


async def test_the_gateway_erases_through_llama_swaps_upstream_passthrough() -> None:
    seen: list[tuple[str, str]] = []
    status = {"code": 200}

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/running":
            return httpx.Response(200, json={"running": [{"model": MODEL}]})
        seen.append((req.method, str(req.url)))
        return httpx.Response(status["code"], json={"id_slot": 7, "n_erased": 1})

    gw = LocalGatewayClient("http://gw:8080/v1", transport=httpx.MockTransport(handle))
    assert await gw.erase_slot(MODEL, 7) is True
    assert seen == [("POST", f"http://gw:8080/upstream/{MODEL}/slots/7?action=erase")]
    status["code"] = 501
    assert await gw.erase_slot(MODEL, 7) is False
    status["code"] = 500
    with pytest.raises(LocalGatewayError):
        await gw.erase_slot(MODEL, 7)
    with pytest.raises(LocalGatewayError):
        await gw.erase_slot("not-resident", 0)


def _crowded() -> list[dict[str, object]]:
    # 7 x 140k idle = 980k; a 200k call needs about 131k of it freed.
    return _layout(**{f"s{i}": _slot(i, 140_000) for i in range(1, 8)})


async def test_a_prefilling_slot_is_charged_its_cap_and_a_decoding_one_its_budget() -> None:
    # While prefilling, `n_prompt_tokens` grows a batch at a time: 5k now says nothing about a
    # 200k prompt still being eaten, so each slot is held at its cap.
    prefilling = _layout(
        **{f"s{i}": _slot(i, 5_000, busy=True, remain=1_000) for i in (0, 1, 2, 3)}
    )
    with pytest.raises(KvPoolBusyError):
        async with _guard(_Gateway(prefilling), wait_s=0.0).placed(
            MODEL, POOL, SlotRole.JCODE, prompt_tokens=250_000, max_tokens=10_000
        ):
            pass
    decoding = _layout(
        **{f"s{i}": _slot(i, 5_000, busy=True, remain=1_000, decoded=10) for i in (0, 2, 3)}
    )
    async with _guard(_Gateway(decoding), wait_s=0.0).placed(
        MODEL, POOL, SlotRole.JCODE, prompt_tokens=250_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == POOL.slot(SlotRole.JCODE)


async def test_an_erase_that_fails_or_stalls_moves_on_to_the_next_candidate() -> None:
    # SMALL's erase raises and PET's never returns (the slot turned busy and llama-server
    # deferred it); neither counts as freed, and WORKSHOP is erased instead.
    gw = _Gateway(_crowded(), refuse=frozenset({7}), stall=frozenset({6}))
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=190_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == 0
    assert gw.erased == [7, 6, 5]
    assert gw.cleared == {5}


async def test_cells_freed_are_what_the_server_reports_after_the_erase() -> None:
    # The erase "succeeds" but the re-read shows the slot still full (it was picked up in
    # between): the guard must keep going rather than trust its own arithmetic.
    gw = _Gateway(_crowded())
    real_erase = gw.erase

    async def erase_without_effect(model: str, slot: int) -> bool:
        ok = await real_erase(model, slot)
        if slot == 7:
            gw.cleared.discard(7)
        return ok

    guard = KvPoolGuard(gw.read, erase_without_effect, wait_s=10.0)
    async with guard.placed(
        MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=190_000, max_tokens=10_000
    ):
        pass
    assert gw.erased == [7, 6]


async def test_a_501_is_retried_once_the_ttl_lapses() -> None:
    clock = _Clock()
    gw = _Gateway(_crowded(), erase_ok=False)
    guard = _guard(gw, clock=clock)
    call: dict[str, Any] = {"prompt_tokens": 190_000, "max_tokens": 10_000}
    async with guard.placed(MODEL, POOL, SlotRole.INTERACTIVE, **call):
        pass
    async with guard.placed(MODEL, POOL, SlotRole.INTERACTIVE, **call):
        pass
    assert gw.erased == [7]
    clock.now += NO_ERASE_TTL_S + 1
    async with guard.placed(MODEL, POOL, SlotRole.INTERACTIVE, **call):
        pass
    assert gw.erased == [7, 7]


async def test_the_pet_stays_put_when_the_call_is_too_big_for_the_small_slot() -> None:
    pool = dataclasses.replace(
        POOL,
        reservations=tuple(
            dataclasses.replace(r, cap_tokens=8_192) if r.role is SlotRole.SMALL else r
            for r in POOL.reservations
        ),
    )
    gw = _Gateway(_layout(s6=_slot(6, 5_000, busy=True)))
    async with _guard(gw).placed(
        MODEL, pool, SlotRole.PET, prompt_tokens=20_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == pool.slot(SlotRole.PET)


async def test_two_contending_calls_do_not_both_count_the_same_free_cells() -> None:
    # Three placed calls hold 3 x 256k. JCODE's 256k fits what is left exactly, INGEST's 128k
    # fits on its own too, but not both: whichever decides second must see the first.
    gw = _Gateway(_layout())
    guard = _guard(gw, wait_s=0.0)
    full: dict[str, Any] = {"prompt_tokens": 252_144, "max_tokens": 10_000}
    release = asyncio.Event()

    async def hold(role: SlotRole, **call: Any) -> str:
        try:
            async with guard.placed(MODEL, POOL, role, **call):
                await release.wait()
        except KvPoolBusyError:
            return "busy"
        return "ok"

    holders = [
        asyncio.create_task(hold(r, **full))
        for r in (SlotRole.RESEARCH, SlotRole.SCHEDULED, SlotRole.INTERACTIVE)
    ]
    contenders = [
        asyncio.create_task(hold(SlotRole.JCODE, **full)),
        asyncio.create_task(hold(SlotRole.INGEST, prompt_tokens=120_000, max_tokens=10_000)),
    ]
    await asyncio.wait(contenders, return_when=asyncio.FIRST_COMPLETED)
    release.set()
    results = await asyncio.gather(*holders, *contenders)
    assert results[:3] == ["ok", "ok", "ok"]
    assert sorted(results[3:]) == ["busy", "ok"]
    assert guard._pending == {}  # every placement released on the way out


class _FlakyReads:
    """`/slots` that answers with `body` until `fail` is set, then raises (or hangs)."""

    def __init__(self, body: list[dict[str, object]], *, hang: bool = False) -> None:
        self.body = body
        self.fail = False
        self._hang = hang
        self.erased: list[int] = []

    async def read(self, model: str) -> list[dict[str, object]]:
        if self.fail:
            if self._hang:
                await asyncio.Event().wait()
            raise LocalGatewayError("down")
        return self.body

    async def erase(self, model: str, slot: int) -> bool:
        self.erased.append(slot)
        return True


async def test_a_slots_read_that_hangs_is_bounded_and_reads_as_unreadable() -> None:
    gw = _FlakyReads(_layout(), hang=True)
    gw.fail = True
    guard = KvPoolGuard(gw.read, gw.erase, read_timeout_s=0.01)
    async with guard.placed(
        MODEL, POOL, SlotRole.SMALL, prompt_tokens=100, max_tokens=100
    ) as placed:
        assert placed.slot is None  # never seen a matching layout: nothing to trust


async def test_an_unreadable_read_after_a_recent_match_stays_pinned_until_the_ttl() -> None:
    clock = _Clock()
    gw = _FlakyReads(_layout())
    guard = KvPoolGuard(gw.read, gw.erase, sleep=clock.sleep, clock=clock)
    call: dict[str, Any] = {"prompt_tokens": 1_000, "max_tokens": 500}
    async with guard.placed(MODEL, POOL, SlotRole.RESEARCH, **call) as placed:
        assert placed.slot == POOL.slot(SlotRole.RESEARCH)
    gw.fail = True
    clock.now += LAYOUT_TTL_S - 1
    async with guard.placed(MODEL, POOL, SlotRole.RESEARCH, **call) as placed:
        # Pinned off the recent match, and recorded so the next decision counts it.
        assert placed.slot == POOL.slot(SlotRole.RESEARCH)
        assert guard._pending_on(MODEL, POOL.slot(SlotRole.RESEARCH)) == 1_500
    assert gw.erased == []
    clock.now += 2
    async with guard.placed(MODEL, POOL, SlotRole.RESEARCH, **call) as placed:
        assert placed.slot is None


async def test_a_mismatched_read_forgets_the_last_good_layout() -> None:
    gw = _FlakyReads(_layout())
    guard = KvPoolGuard(gw.read, gw.erase)
    call: dict[str, Any] = {"prompt_tokens": 1_000, "max_tokens": 500}
    async with guard.placed(MODEL, POOL, SlotRole.SMALL, **call) as placed:
        assert placed.slot == POOL.slot(SlotRole.SMALL)
    gw.body = [_slot(i) for i in range(4)]
    async with guard.placed(MODEL, POOL, SlotRole.SMALL, **call) as placed:
        assert placed.slot is None
    gw.fail = True
    async with guard.placed(MODEL, POOL, SlotRole.SMALL, **call) as placed:
        assert placed.slot is None


async def test_a_slot_that_turns_busy_before_its_erase_is_skipped() -> None:
    # The read that chose SMALL showed it idle; the read right before its erase shows it
    # running, so the next candidate (PET) is erased instead.
    now_busy = {**_crowded()[7], "is_processing": True, "next_token": [{"n_decoded": 5}]}
    gw = _Gateway(_crowded(), [*_crowded()[:7], now_busy, *_crowded()[8:]])
    async with _guard(gw).placed(
        MODEL, POOL, SlotRole.INTERACTIVE, prompt_tokens=190_000, max_tokens=10_000
    ) as placed:
        assert placed.slot == 0
    assert gw.erased == [6]


def _jerv_last_resort(*, jerv_busy: bool) -> list[dict[str, object]]:
    # Busy slots 1-5 hold 960k; jerv's idle 250k is the only cell left to free.
    held = {1: 125_000, 2: 230_000, 3: 230_000, 4: 250_000, 5: 125_000}
    slots = {f"s{i}": _slot(i, n, busy=True, decoded=1) for i, n in held.items()}
    return _layout(s0=_slot(0, 250_000, busy=jerv_busy, decoded=1), **slots)


async def test_jervs_slot_is_erased_only_off_a_read_taken_just_before() -> None:
    gw = _Gateway(_jerv_last_resort(jerv_busy=False), _jerv_last_resort(jerv_busy=True))
    with pytest.raises(KvPoolBusyError):
        async with _guard(gw, wait_s=0.0).placed(
            MODEL, POOL, SlotRole.SMALL, prompt_tokens=50_000, max_tokens=10_000
        ):
            pass
    assert gw.erased == []
    idle = _Gateway(_jerv_last_resort(jerv_busy=False))
    async with _guard(idle).placed(
        MODEL, POOL, SlotRole.SMALL, prompt_tokens=50_000, max_tokens=10_000
    ):
        pass
    assert idle.erased == [0]


async def test_an_unreadable_pool_erases_nothing() -> None:
    gw = _FlakyReads(_jerv_last_resort(jerv_busy=False))
    real = gw.read

    async def once_then_down(model: str) -> list[dict[str, object]]:
        body = await real(model)
        gw.fail = True
        return body

    # Jerv's slot would be freed, but the read before its erase fails: wait, never erase blind.
    clock = _Clock()
    guard = KvPoolGuard(once_then_down, gw.erase, wait_s=10.0, sleep=clock.sleep, clock=clock)
    with pytest.raises(KvPoolBusyError):
        async with guard.placed(
            MODEL, POOL, SlotRole.SMALL, prompt_tokens=50_000, max_tokens=10_000
        ):
            pass
    assert gw.erased == []


async def test_the_pet_overflows_when_its_own_slot_holds_a_call_this_process_placed() -> None:
    # The first call reads idle until llama-server picks it up; the second must not queue
    # behind it in the same slot.
    guard = _guard(_Gateway(_layout()))
    call: dict[str, Any] = {"prompt_tokens": 2_000, "max_tokens": 500}
    async with guard.placed(MODEL, POOL, SlotRole.PET, **call) as first:
        assert first.slot == POOL.slot(SlotRole.PET)
        async with guard.placed(MODEL, POOL, SlotRole.PET, **call) as second:
            assert second.slot == POOL.slot(SlotRole.SMALL)


async def test_a_call_of_ours_still_prefilling_is_charged_its_own_size_not_the_cap() -> None:
    # Our jcode call (30k) is prefilling in slot 4. Charged its 256k cap, the 720k idle in
    # slots 0/2/3 plus this 110k call would overrun and force an erase; charged 30k, it fits.
    crowded = _layout(
        s0=_slot(0, 240_000),
        s2=_slot(2, 240_000),
        s3=_slot(3, 240_000),
        s4=_slot(4, 5_000, busy=True, remain=10_000),
    )
    gw = _Gateway(_layout(), crowded)
    guard = _guard(gw, wait_s=0.0)
    async with (
        guard.placed(MODEL, POOL, SlotRole.JCODE, prompt_tokens=20_000, max_tokens=10_000),
        guard.placed(
            MODEL, POOL, SlotRole.INGEST, prompt_tokens=100_000, max_tokens=10_000
        ) as placed,
    ):
        assert placed.slot == POOL.slot(SlotRole.INGEST)
    assert gw.erased == []


async def test_a_per_call_wait_overrides_the_guards_default() -> None:
    clock = _Clock()
    gw = _Gateway(_busy_pool())
    with pytest.raises(KvPoolBusyError):
        async with _guard(gw, wait_s=100.0, clock=clock).placed(
            MODEL,
            POOL,
            SlotRole.INTERACTIVE,
            prompt_tokens=50_000,
            max_tokens=10_000,
            wait_s=4.0,
        ):
            pass
    assert clock.now <= 6.0


# --- disk restores fitted through the guard (FLASH_NEXT F4) -------------------------------------


def _never_run(sid: int) -> dict[str, object]:
    # A slot that never ran a request: llama-server sends no `n_prompt_tokens` at all.
    return {"id": sid, "n_ctx": 262_144, "is_processing": False}


async def test_a_restore_fits_only_beside_the_calls_this_process_has_placed() -> None:
    gw = _Gateway(_layout(s1=_slot(1, 100_000)))
    guard = _guard(gw)
    assert await guard.fits(MODEL, POOL, 2, 100_000)
    async with guard.placed(MODEL, POOL, SlotRole.RESEARCH, prompt_tokens=200_000, max_tokens=4):
        # The placed research call is not running yet (it reads idle), but it is counted.
        assert not await guard.fits(MODEL, POOL, 2, 800_000)
        assert await guard.fits(MODEL, POOL, 2, 100_000)


async def test_a_restored_never_run_slot_is_charged_until_it_reports_or_is_erased() -> None:
    restored = [_never_run(i) if i == 2 else _slot(i) for i in range(POOL.n_slots)]
    restored[1] = _slot(1, 700_000)
    gw = _Gateway(restored)
    guard = _guard(gw)
    erased: list[tuple[str, int]] = []
    guard.add_erase_listener(lambda model, slot: erased.append((model, slot)))
    guard.note_restored(MODEL, 2, 300_000)
    # 700k + the 300k restored into slot 2 leaves no room for 100k more.
    assert not await guard.fits(MODEL, POOL, 3, 100_000)
    # A call that needs the room frees the restored slot like any idle one, and says so.
    async with guard.placed(MODEL, POOL, SlotRole.SMALL, prompt_tokens=50_000, max_tokens=1_000):
        pass
    assert 2 in gw.erased and (MODEL, 2) in erased
    assert await guard.fits(MODEL, POOL, 3, 100_000)


async def test_a_reload_forgets_what_was_restored() -> None:
    gw = _Gateway([_never_run(i) for i in range(POOL.n_slots)])
    guard = _guard(gw)
    guard.note_restored(MODEL, 2, 1_000_000)
    assert not await guard.fits(MODEL, POOL, 3, 100_000)
    guard.forget_restored(MODEL)
    assert await guard.fits(MODEL, POOL, 3, 100_000)


# ---- the chat pair's re-warm erase and eviction order ----------------------------------------


async def test_a_restore_erase_goes_only_to_an_idle_slot_with_no_other_call_placed() -> None:
    told: list[tuple[str, int]] = []
    gw = _Gateway(_layout(s0=_slot(0, 47_000)))
    guard = _guard(gw)
    guard.add_erase_listener(lambda m, s: told.append((m, s)))
    ticket = await guard.reserve_restore(MODEL, POOL, 0, 29_000)
    assert ticket is not None
    # Another call of this process is placed on the slot: refused.
    guard._hold(MODEL, 0, ticket + 1000, 5_000)
    assert not await guard.erase_for_restore(MODEL, POOL, 0, ticket)
    guard.end_restore(MODEL, 0, ticket + 1000)
    # Only the restore's own hold: erased, and the store is told.
    assert await guard.erase_for_restore(MODEL, POOL, 0, ticket)
    assert gw.erased == [0] and told == [(MODEL, 0)]
    guard.end_restore(MODEL, 0, ticket)


async def test_a_restore_erase_never_reaches_a_busy_slot_or_a_stale_layout() -> None:
    busy = _Gateway(_layout(s0=_slot(0, 47_000, busy=True, decoded=5)))
    assert not await _guard(busy).erase_for_restore(MODEL, POOL, 0, 1)
    assert busy.erased == []
    stale = _Gateway([_slot(i) for i in range(4)])
    assert not await _guard(stale).erase_for_restore(MODEL, POOL, 0, 1)
    assert stale.erased == []


async def test_a_server_that_cannot_erase_is_remembered() -> None:
    gw = _Gateway(_layout(s0=_slot(0, 47_000)), erase_ok=False)
    guard = _guard(gw)
    assert not await guard.erase_for_restore(MODEL, POOL, 0, 1)
    assert guard._cannot_erase(MODEL)
    refused = _Gateway(_layout(s0=_slot(0, 47_000)), refuse=frozenset({0}))
    assert not await _guard(refused).erase_for_restore(MODEL, POOL, 0, 1)


async def test_the_pool_frees_the_latest_chat_last_whichever_pair_slot_holds_it() -> None:
    guard = _guard(_Gateway(_layout()))
    guard.set_keep_last(lambda _m: 0)
    assert guard._eviction_order(MODEL, POOL)[-2:] == [9, 0]

    def broken(_m: str) -> int | None:
        raise RuntimeError("store gone")

    guard.set_keep_last(broken)
    assert guard._eviction_order(MODEL, POOL) == POOL.eviction_order()


async def test_an_eviction_spares_the_latest_chat_until_the_warm_slot_is_gone() -> None:
    # Only the two chat slots hold anything, and the call needs one of them gone. Whichever
    # holds the latest chat stays; the other — the warm prefix, restorable from disk — goes.
    gw = _Gateway(_layout(s0=_slot(0, 520_000), s9=_slot(9, 520_000)))
    guard = _guard(gw)
    guard.set_keep_last(lambda _m: 0)
    async with guard.placed(MODEL, POOL, SlotRole.RESEARCH, prompt_tokens=40_000, max_tokens=4_000):
        pass
    assert gw.erased == [9]
    gw2 = _Gateway(_layout(s0=_slot(0, 520_000), s9=_slot(9, 520_000)))
    guard2 = _guard(gw2)
    guard2.set_keep_last(lambda _m: 9)
    async with guard2.placed(
        MODEL, POOL, SlotRole.RESEARCH, prompt_tokens=40_000, max_tokens=4_000
    ):
        pass
    assert gw2.erased == [0]
