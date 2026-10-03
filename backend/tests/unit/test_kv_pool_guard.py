"""The pool guard: what it projects from `/slots`, which slots it frees and in what order,
when it waits, and how it degrades (a stale layout, an unreadable `/slots`, a server that
cannot erase). Faked `/slots` bodies in the shape llama-server sends (see test_prefill.py)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from jbrain.llm.kv_pool_guard import KvPoolBusyError, KvPoolGuard
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, SlotCapError, SlotRole

POOL = FLASH_NEXT_POOL
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
    """Eight slots, empty unless named (`s3=_slot(3, ...)`)."""
    return [held.get(f"s{i}", _slot(i)) for i in range(POOL.n_slots)]


class _Gateway:
    def __init__(self, *reads: list[dict[str, object]], erase_ok: bool = True) -> None:
        self._reads = list(reads)
        self.reads = 0
        self.erased: list[int] = []
        self._erase_ok = erase_ok

    async def read(self, model: str) -> list[dict[str, object]]:
        assert model == MODEL
        body = self._reads[min(self.reads, len(self._reads) - 1)]
        self.reads += 1
        return body

    async def erase(self, model: str, slot: int) -> bool:
        self.erased.append(slot)
        return self._erase_ok


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _guard(gw: _Gateway, *, wait_s: float = 10.0) -> KvPoolGuard:
    clock = _Clock()
    return KvPoolGuard(gw.read, gw.erase, wait_s=wait_s, poll_s=2.0, sleep=clock.sleep, clock=clock)


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
