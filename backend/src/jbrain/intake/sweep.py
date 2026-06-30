"""The guided-intake background reaper (W3).

A periodic sweep that transitions stale `drafting` intake sessions to `abandoned`
(§6): a session whose last turn (or open, if it never had one) is older than the
window is presumed walked-away-from. An abandoned `open` KEEPS its `opens_used`
slot — the slot is spent at redeem and is not reclaimed. Wired as a lifespan task in
`main.py`, the sibling of the tasks scheduler loop.
"""

from __future__ import annotations

import asyncio

import structlog

from jbrain.db.session import SessionContext
from jbrain.intake.service import IntakeRepo

log = structlog.get_logger()

# Sweep cadence and the idle window after which a drafting session is abandoned.
REAP_INTERVAL_SECONDS = 15 * 60
ABANDON_AFTER_SECONDS = 2 * 60 * 60


async def intake_reaper_loop(
    repo: IntakeRepo,
    ctx: SessionContext,
    *,
    interval_seconds: int = REAP_INTERVAL_SECONDS,
    older_than_seconds: int = ABANDON_AFTER_SECONDS,
) -> None:
    """Reap stale drafting sessions forever, sleeping `interval_seconds` between sweeps.

    A sweep failure is logged and the loop continues (a transient DB hiccup must not kill
    the reaper); cancellation propagates so shutdown can stop it cleanly."""
    while True:
        try:
            reaped = await repo.reap_abandoned(ctx, older_than_seconds)
            if reaped:
                log.info("intake.reaper.swept", abandoned=reaped)
        except Exception:
            log.exception("intake.reaper.failed")
        await asyncio.sleep(interval_seconds)
