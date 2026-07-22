"""The deepest-research background execution lane (DEEPEST_RESEARCH_TOOL_PLAN.md, R3):
detached, concurrent runs with a wall-clock watchdog. Proven with plain coroutines — no
DB, no LLM, no tree — so the assertions are purely about the concurrency contract: launch
never blocks, runs proceed in parallel, the watchdog kills a runaway, and a slot always
frees (completion, error, cancel, drain)."""

import asyncio

from jbrain.agent.deepest_lane import DeepestRunLane


async def _settle(lane: DeepestRunLane, want: int, *, max_wait: float = 1.0) -> None:
    """Yield the loop until the lane has `want` active runs (deregistration is async)."""
    remaining = max_wait
    while lane.active() != want and remaining > 0:
        await asyncio.sleep(0.01)
        remaining -= 0.01
    assert lane.active() == want


async def test_launch_is_non_blocking_and_tracks_the_run() -> None:
    """`launch` returns immediately with the run tracked — the caller (the kickoff tool)
    gets its turn back while the run proceeds in the background."""
    lane = DeepestRunLane()
    started, release = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        started.set()
        await release.wait()

    assert lane.launch("r1", run, wall_clock_s=100) is True
    assert lane.active() == 1 and lane.is_running("r1")  # tracked before the loop yields
    await asyncio.wait_for(started.wait(), 1)  # the detached task genuinely runs
    release.set()
    await _settle(lane, 0)  # completes and frees its slot


async def test_two_runs_proceed_concurrently() -> None:
    """With a pool of 2, two runs are in flight at once — genuinely concurrent, not
    serialized behind one another (the property the sequential loops lack)."""
    lane = DeepestRunLane(max_concurrent=2)
    up = asyncio.Event()

    async def run() -> None:
        await up.wait()

    assert lane.launch("a", run, wall_clock_s=100) is True
    assert lane.launch("b", run, wall_clock_s=100) is True
    assert lane.active() == 2
    up.set()
    await _settle(lane, 0)


async def test_at_capacity_refuses_rather_than_blocks() -> None:
    """The default single-slot lane refuses a second run (open decision §9.4) — a clean
    False the caller surfaces as 'a run is already going', never a block or a queue."""
    lane = DeepestRunLane()  # max_concurrent = 1
    up = asyncio.Event()

    async def run() -> None:
        await up.wait()

    assert lane.launch("a", run, wall_clock_s=100) is True
    assert lane.launch("b", run, wall_clock_s=100) is False  # refused, immediately
    assert lane.active() == 1
    up.set()
    await _settle(lane, 0)


async def test_duplicate_run_id_refused() -> None:
    lane = DeepestRunLane(max_concurrent=4)
    up = asyncio.Event()

    async def run() -> None:
        await up.wait()

    assert lane.launch("dup", run, wall_clock_s=100) is True
    assert lane.launch("dup", run, wall_clock_s=100) is False
    up.set()
    await _settle(lane, 0)


async def test_watchdog_cancels_a_run_past_its_wall_clock() -> None:
    """A run that outlives its ceiling is cancelled by the watchdog and its slot freed —
    the backstop for a run that runs outside the /chat and worker timeouts entirely."""
    lane = DeepestRunLane()
    cancelled = asyncio.Event()

    async def run() -> None:
        try:
            await asyncio.Event().wait()  # never completes on its own
        except asyncio.CancelledError:
            cancelled.set()
            raise

    assert lane.launch("slow", run, wall_clock_s=0.05) is True
    await _settle(lane, 0)  # watchdog fired, slot freed
    assert cancelled.is_set()


async def test_a_failing_run_never_crashes_the_lane() -> None:
    """A run that raises is swallowed and its slot freed — a background failure is logged,
    not propagated, and the lane stays usable for the next run."""
    lane = DeepestRunLane()

    async def boom() -> None:
        raise RuntimeError("kaboom")

    assert lane.launch("bad", boom, wall_clock_s=100) is True
    await _settle(lane, 0)
    # The lane still accepts a new run after the failure.
    up = asyncio.Event()

    async def ok() -> None:
        await up.wait()

    assert lane.launch("next", ok, wall_clock_s=100) is True
    up.set()
    await _settle(lane, 0)


async def test_cancel_and_drain_settle_in_flight_runs() -> None:
    lane = DeepestRunLane(max_concurrent=3)
    up = asyncio.Event()

    async def run() -> None:
        await up.wait()

    lane.launch("a", run, wall_clock_s=100)
    lane.launch("b", run, wall_clock_s=100)
    lane.launch("c", run, wall_clock_s=100)
    assert lane.active() == 3
    assert await lane.cancel("a") is True
    assert await lane.cancel("missing") is False
    assert lane.active() == 2
    await lane.drain()
    assert lane.active() == 0
