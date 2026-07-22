"""The deepest-research background execution lane (docs/plans/DEEPEST_RESEARCH_TOOL_PLAN.md,
R3).

A deepest run is minutes-to-hours of work, so it cannot run inline on either sequential
loop the box already has — the job worker (`workflow/worker.py`) and the scheduled-task
tick (`tasks/scheduler.py`) each `await` their work one item at a time, so a long run
there would stall ingest / the morning brief / everything else. This lane instead runs a
run as a **detached, genuinely concurrent** `asyncio.Task`: `launch` returns immediately
(the caller — the `deepest_research` kickoff tool — enqueues and gets its turn back), the
run proceeds in the background, and a **watchdog** cancels one that outlives its
wall-clock ceiling (it runs outside both the `/chat` turn timeout and the worker's job
machinery, so nothing else would stop a runaway).

The lane is deliberately generic: it supervises an opaque `run()` coroutine and knows
nothing about how the run's context is built (the trusted `TreeState`-seeding context is
R4) or what it does (drive `DeepResearchService`, checkpoint, notify — R4–R7). That keeps
it unit-testable with no DB, no LLM, no tree — just the concurrency contract.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger()

# One deepest run at a time by default (open decision §9.4: a global lock is the simplest
# safe choice; a small pool is a config bump). A second `launch` while at capacity is
# refused, not queued — the kickoff tool tells the owner a run is already in flight.
DEFAULT_MAX_CONCURRENT = 1


class DeepestRunLane:
    """Runs deepest-research runs as detached, concurrent background tasks, each tracked
    by `run_id` and bounded by a wall-clock watchdog. `launch` never blocks the caller;
    the run's own coroutine owns recording + notifying on completion, cancel, or error."""

    def __init__(self, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._runs: dict[str, asyncio.Task[None]] = {}

    def active(self) -> int:
        """How many runs are in flight right now."""
        return len(self._runs)

    def is_running(self, run_id: str) -> bool:
        return run_id in self._runs

    def launch(
        self,
        run_id: str,
        run: Callable[[], Awaitable[None]],
        *,
        wall_clock_s: float,
    ) -> bool:
        """Start `run()` as a detached background task bounded by `wall_clock_s`. Returns
        immediately: True once the task is scheduled, False if the lane is at capacity or
        `run_id` is already in flight (the caller surfaces "a run is already going" — never
        a block). The `run` coroutine is responsible for its own persistence + owner
        notification; the lane only supervises its lifetime."""
        if run_id in self._runs:
            log.info("deepest_lane.duplicate", run_id=run_id)
            return False
        if len(self._runs) >= self._max_concurrent:
            log.info("deepest_lane.at_capacity", run_id=run_id, active=len(self._runs))
            return False
        task = asyncio.create_task(self._supervise(run_id, run, wall_clock_s))
        self._runs[run_id] = task
        return True

    async def _supervise(
        self, run_id: str, run: Callable[[], Awaitable[None]], wall_clock_s: float
    ) -> None:
        """Own one run's lifetime: enforce the wall-clock watchdog, swallow its failure
        (a background run must never crash the lane), and always deregister so a slot frees
        and a later run can start. Cancellation (lane shutdown / explicit cancel) propagates
        so the task settles as cancelled, not swallowed."""
        try:
            await asyncio.wait_for(run(), timeout=wall_clock_s)
        except TimeoutError:
            log.warning("deepest_lane.watchdog_cancelled", run_id=run_id, wall_clock_s=wall_clock_s)
        except asyncio.CancelledError:
            log.info("deepest_lane.cancelled", run_id=run_id)
            raise
        except Exception:  # noqa: BLE001 — a background run's failure never crashes the lane
            log.warning("deepest_lane.run_failed", run_id=run_id, exc_info=True)
        finally:
            self._runs.pop(run_id, None)

    async def cancel(self, run_id: str) -> bool:
        """Cancel an in-flight run and await its settlement. Returns False if unknown."""
        task = self._runs.get(run_id)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Deregister defensively: a task cancelled BEFORE it ever ran never enters
        # `_supervise`'s try, so its `finally` never fires — free the slot here so cancel
        # (and drain) always leave the lane consistent, not just the run-to-completion path.
        self._runs.pop(run_id, None)
        return True

    async def drain(self) -> None:
        """Cancel every in-flight run and await settlement — the shutdown hook so a
        process stop tears down background runs cleanly instead of stranding tasks."""
        for run_id in list(self._runs):
            await self.cancel(run_id)
