"""Worker entrypoint: the job-queue loop.

Single-threaded by design at personal scale: claim one job, run it, repeat.
Startup backfill enqueues ingestion for every note still marked 'pending'
(migration 0003 stamps all pre-existing notes with it), so the index
self-heals after upgrades without manual intervention. The heartbeat log
line keeps the service honest in `docker compose ps` and the Ops screen.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jbrain import queue
from jbrain.config import get_settings
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.storage import FsBlobStore

log = structlog.get_logger()

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 60

Handler = Callable[[dict[str, Any]], Awaitable[None]]


async def process_one(
    maker: async_sessionmaker[AsyncSession], handlers: dict[str, Handler]
) -> bool:
    """Claim and run a single job; returns False when the queue is idle."""
    job = await queue.claim(maker, queue.SYSTEM_CTX)
    if job is None:
        return False
    handler = handlers.get(job.kind)
    if handler is None:
        # Unknown kinds are config drift, not transient errors — retrying
        # them anyway surfaces the problem in attempts/last_error.
        await queue.fail(maker, queue.SYSTEM_CTX, job.id, f"no handler for kind '{job.kind}'")
        log.error("worker.job_unhandled", job_id=job.id, kind=job.kind)
        return True
    try:
        await handler(job.payload)
    except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
        await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc))
        log.warning("worker.job_failed", job_id=job.id, kind=job.kind, error=repr(exc))
    else:
        await queue.complete(maker, queue.SYSTEM_CTX, job.id)
        log.info("worker.job_done", job_id=job.id, kind=job.kind)
    return True


async def run_loop(maker: async_sessionmaker[AsyncSession], handlers: dict[str, Handler]) -> None:
    backfilled = False
    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            log.info("worker.heartbeat")
            last_heartbeat = now
        try:
            if not backfilled:
                enqueued = await queue.backfill_pending_notes(maker, queue.SYSTEM_CTX)
                backfilled = True
                log.info("worker.backfill", enqueued=enqueued)
            if await process_one(maker, handlers):
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - survive DB blips, like the old heartbeat
            log.warning("worker.loop_error", error=repr(exc))
        await asyncio.sleep(POLL_SECONDS)


async def run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    pipeline = IngestPipeline(maker, FsBlobStore(settings.blob_dir))
    handlers: dict[str, Handler] = {"ingest_note": pipeline.ingest_note}
    try:
        await run_loop(maker, handlers)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
