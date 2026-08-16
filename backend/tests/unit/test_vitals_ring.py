"""The in-process GPU history behind the vitals graph's 1/5/15-minute windows.

Kept in memory rather than in app.host_metrics on purpose: that table samples every 30
seconds for a 30-day graph, so a one-minute window read from it is two points, and
writing a row a second to serve a fifteen-minute view would multiply it thirty-fold to
answer a question that stops mattering a quarter of an hour later.
"""

import asyncio
import contextlib
import time

import pytest

from jbrain.vitals_ring import RING_SAMPLES, VitalsRing, sample_loop


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
