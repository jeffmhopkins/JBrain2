"""The in-process GPU history behind the vitals graph's 1/5/15-minute windows.

Kept in memory rather than in app.host_metrics on purpose: that table samples every 30
seconds for a 30-day graph, so a one-minute window read from it is two points, and
writing a row a second to serve a fifteen-minute view would multiply it thirty-fold to
answer a question that stops mattering a quarter of an hour later.
"""

import asyncio
import contextlib
import time
from typing import cast

import pytest

from jbrain.vitals_ring import (
    RING_SAMPLES,
    VitalsRing,
    sample_loop,
    seconds_to_next_tick,
    tick_for,
)


def test_keeps_samples_inside_the_window() -> None:
    ring = VitalsRing()
    ring.record(1000.0, 40.0)
    ring.record(1500.0, 90.0)

    inside = ring.since(seconds=600, now=1600.0)

    assert [s["gpu"] for s in inside] == [40.0, 90.0]
    # Epoch MILLIseconds, so the browser can merge them with its own ring directly.
    assert inside[0]["at_ms"] == 1_000_000


def test_drops_samples_older_than_the_window() -> None:
    ring = VitalsRing()
    ring.record(1000.0, 40.0)
    ring.record(1900.0, 90.0)

    assert [s["gpu"] for s in ring.since(seconds=120, now=1950.0)] == [90.0]


def test_keeps_a_gap_rather_than_a_zero() -> None:
    # A zero would read as an idle box; the gauge simply could not be read.
    ring = VitalsRing()
    ring.record(1000.0, None)

    assert ring.since(seconds=60, now=1010.0)[0]["gpu"] is None


def test_bounds_its_own_memory() -> None:
    ring = VitalsRing(size=4)
    for i in range(10):
        ring.record(float(i), float(i))

    kept = ring.since(seconds=100, now=10.0)

    assert [s["gpu"] for s in kept] == [6.0, 7.0, 8.0, 9.0]


def test_ring_holds_the_widest_window_the_graph_offers() -> None:
    # 15 minutes at one sample a second.
    assert RING_SAMPLES == 900


async def test_sample_loop_records_and_survives_a_failing_gauge() -> None:
    """A gauge that starts raising must not stop the sampler — a loop that dies on the
    first error keeps no history at all.

    Driven by the READS rather than by the clock: the fourth call cancels, so the loop
    runs an exact number of times. Sleeping for a fixed span instead made the iteration
    count depend on machine load, and with a fast interval the ring would overflow and
    evict the very sample under assertion.
    """
    ring = VitalsRing()
    calls = 0

    def read() -> float | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 50.0
        if calls == 2:
            return None  # the gauge is there, but has nothing to say
        if calls == 3:
            raise RuntimeError("gauge went away")
        raise asyncio.CancelledError  # ends the loop deterministically

    with pytest.raises(asyncio.CancelledError):
        await sample_loop(ring, read=read, interval=0)

    kept = [s["gpu"] for s in ring.since(seconds=600)]
    # A reading, a gap, then a gap FROM THE RAISING GAUGE — and a fourth call happened,
    # which is the proof the loop carried on past the error.
    assert kept == [50.0, None, None]
    assert calls == 4


async def test_sample_loop_survives_a_gauge_that_hangs() -> None:
    """A blocking sysfs read is the one thing in this loop that can stall the whole API:
    it runs once a second for the life of the process, on the same event loop that serves
    every request. A hung amdgpu attribute must cost one sample, not the box."""
    ring = VitalsRing()
    calls = 0

    def hangs() -> float | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(5)  # far past READ_TIMEOUT_SECONDS
            return 99.0
        raise asyncio.CancelledError

    with contextlib.suppress(asyncio.CancelledError):
        await sample_loop(ring, read=hangs, interval=0)

    samples = ring.since(3600)
    # The hung read was abandoned as a gap rather than blocking the loop behind it.
    assert samples[0]["gpu"] is None
    assert calls == 2  # the loop kept going


async def test_sample_loop_does_not_block_the_event_loop() -> None:
    """The read runs in a THREAD, so other tasks keep being scheduled while it is stuck.

    Asserted while the slow read is still in flight, not after: awaiting the other task
    first would let it finish either way and the test would pass against an inline read —
    which is precisely the bug. In production the "other task" is every request the API
    is serving."""
    ring = VitalsRing()
    ticks = 0

    async def other_work() -> None:
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    calls = 0

    def slow() -> float | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.3)
            return 12.0
        raise asyncio.CancelledError

    task = asyncio.create_task(other_work())
    with contextlib.suppress(asyncio.CancelledError):
        await sample_loop(ring, read=slow, interval=0)

    # 0.3s of read against 0.05s of ticks: threaded, every tick has landed. Inline, the
    # loop never yielded and none had.
    assert ticks == 5
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_tick_snaps_to_the_second_the_reading_belongs_to() -> None:
    """The stamp is the second the sample is a reading OF, not the instant the read
    returned — which is what puts exactly one sample in each of the browser's slots."""
    assert tick_for(1000.004, 1.0) == 1000.0
    # NEAREST, not floor: a loop that wakes a hair early must not stamp the tick it has
    # already recorded and leave the one it woke for empty.
    assert tick_for(999.996, 1.0) == 1000.0
    assert tick_for(1000.6, 1.0) == 1001.0
    # An interval of 0 is the tests' "run flat out" mode; there is no grid to snap to.
    assert tick_for(1000.4, 0) == 1000.4


def test_sleeps_to_the_next_tick_not_for_a_whole_one() -> None:
    """A full interval AFTER the work makes the period interval-plus-work, and the
    emitter slips a whole tick against the wall clock every few dozen ticks."""
    assert seconds_to_next_tick(1.0, now=1000.25) == pytest.approx(0.75)
    # Already exactly on a boundary: wait the whole interval rather than fire twice.
    assert seconds_to_next_tick(1.0, now=1000.0) == pytest.approx(1.0)
    assert seconds_to_next_tick(0, now=1000.25) == 0.0


def test_newer_than_reports_only_what_the_client_lacks() -> None:
    ring = VitalsRing()
    ring.record(1000.0, 10.0)
    ring.record(1001.0, 20.0)
    ring.record(1002.0, 30.0)

    assert [s["gpu"] for s in ring.newer_than(1_000_000)] == [20.0, 30.0]
    # Strictly after: the sample the client was last told about is not resent.
    assert [s["gpu"] for s in ring.newer_than(1_002_000)] == []


def test_newer_than_offers_only_the_newest_to_a_fresh_client() -> None:
    """A client with no history is asking for a READING, not for the ring — the detail
    graph seeds its past from /vitals/history instead."""
    ring = VitalsRing()
    ring.record(1000.0, 10.0)
    ring.record(1001.0, 20.0)

    assert [s["gpu"] for s in ring.newer_than(None)] == [20.0]
    assert VitalsRing().newer_than(None) == []


def test_newer_than_bounds_a_catch_up_burst() -> None:
    ring = VitalsRing()
    for i in range(100):
        ring.record(1000.0 + i, float(i))

    caught_up = ring.newer_than(0, limit=5)

    # The NEWEST five: a client that has been away wants the recent past, not the oldest
    # five seconds of a window that has since scrolled past.
    assert [s["gpu"] for s in caught_up] == [95.0, 96.0, 97.0, 98.0, 99.0]


async def test_sample_loop_holds_the_clock_rather_than_drifting() -> None:
    """One sample per wall-clock tick, whatever a read costs.

    The loop used to sleep a full interval AFTER the read, so its period was the interval
    plus the read and it slipped a whole second against the clock every minute or two.
    The second it slipped past held no sample, and the vitals graph — one slot per second
    — drew that as a dropout in a history nothing had failed to record.

    Asserted as "every stamp is its own exact tick" rather than as a spacing, so a loaded
    machine that genuinely misses a tick doesn't fail the test for the wrong reason."""
    ring = VitalsRing()
    interval = 0.1
    calls = 0

    def slow() -> float | None:
        nonlocal calls
        calls += 1
        if calls > 4:
            raise asyncio.CancelledError
        time.sleep(interval * 0.6)  # more than half the tick, well short of a whole one
        return 50.0

    with contextlib.suppress(asyncio.CancelledError):
        await sample_loop(ring, read=slow, interval=interval)

    stamps = [s["at_ms"] for s in ring.since(600)]
    ticks = [cast(int, at) / (interval * 1000) for at in stamps]
    assert all(tick == pytest.approx(round(tick)) for tick in ticks), stamps
    assert len(set(ticks)) == len(ticks), "two samples landed on the same tick"
