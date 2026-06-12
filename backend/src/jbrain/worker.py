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
from jbrain.analysis import purge
from jbrain.analysis.consolidation import Consolidator
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.config import get_settings
from jbrain.embed import NoteEmbedder, TeiEmbedClient
from jbrain.ingest import ocr
from jbrain.ingest.ocr import OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import build_router
from jbrain.schema import get_registry
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from jbrain.usage import SqlUsageRecorder

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
    except queue.PermanentJobError as exc:
        # Retrying cannot help (e.g. malformed extraction after the re-ask):
        # fail now instead of burning the retry budget.
        exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc), permanent=True)
        log.error("worker.job_failed_permanent", job_id=job.id, kind=job.kind, error=repr(exc))
        await _after_exhaustion(maker, job, exhausted)
    except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
        exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc))
        log.warning("worker.job_failed", job_id=job.id, kind=job.kind, error=repr(exc))
        await _after_exhaustion(maker, job, exhausted)
    else:
        await queue.complete(maker, queue.SYSTEM_CTX, job.id)
        log.info("worker.job_done", job_id=job.id, kind=job.kind)
    return True


async def _after_exhaustion(
    maker: async_sessionmaker[AsyncSession], job: queue.Job, exhausted: bool
) -> None:
    """Kind-specific fallbacks once a job has burned its whole retry budget.

    An exhausted ocr_attachment must not strand its note unanalyzed: the
    ingest gate deferred analysis to OCR work that will now never finish, so
    fall back to body-only analysis (jbrain.ingest.ocr).
    """
    if not exhausted or job.kind != "ocr_attachment":
        return
    attachment_id = job.payload.get("attachment_id")
    if attachment_id is not None:
        await ocr.enqueue_analysis_fallback(maker, str(attachment_id))


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
                ingests = await queue.backfill_pending_notes(maker, queue.SYSTEM_CTX)
                embeds = await queue.backfill_unembedded_notes(maker, queue.SYSTEM_CTX)
                # Notes ingested before extraction shipped never analyze
                # until edited; the missing note_analysis row marks them.
                analyses = await queue.backfill_unanalyzed_notes(maker, queue.SYSTEM_CTX)
                # Relationship edges left unlinked by a sloppy model render their
                # whole statement instead of the object entity; re-analysis binds
                # them via the deterministic linking net.
                relinks = await queue.backfill_unlinked_relationship_facts(maker, queue.SYSTEM_CTX)
                # Notes deleted before the purge cascade shipped left orphaned
                # derived artifacts (incl. resolved review history quoting
                # their text); sweep them once per boot.
                purged = await purge.backfill_deleted_note_artifacts(maker)
                # Normalize predicate drift left by older prompt versions.
                consolidations = await queue.backfill_consolidate(maker, queue.SYSTEM_CTX)
                backfilled = True
                log.info(
                    "worker.backfill",
                    ingest_jobs=ingests,
                    embed_jobs=embeds,
                    analyze_jobs=analyses,
                    relink_jobs=relinks,
                    purged_notes=purged,
                    consolidate_jobs=consolidations,
                )
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
    blobs = FsBlobStore(settings.blob_dir)
    pipeline = IngestPipeline(maker, blobs)
    embedder = NoteEmbedder(maker, TeiEmbedClient(settings.embed_url), settings.embed_model)
    router = build_router(settings, recorder=SqlUsageRecorder(maker))
    # The embed client also powers entity-resolution layer 2 (similarity);
    # without it the resolver still runs layers 1/2b/3.
    analyzer = AnalysisPipeline(
        maker, router, embedder=TeiEmbedClient(settings.embed_url), embed_model=settings.embed_model
    )
    # Eager-load the schema registry so a missing/malformed defs/ fails the
    # worker LOUDLY at startup — never mid-note, where the SchemaError would
    # otherwise re-bill the extraction call on every retry.
    get_registry()
    handlers: dict[str, Handler] = {
        "ingest_note": pipeline.ingest_note,
        "embed_note": embedder.embed_note,
        "analyze_note": analyzer.analyze_note,
        # The vision handler reads the image-analysis mode setting per job.
        "ocr_attachment": OcrPipeline(maker, blobs, router, SqlSettingsStore(maker)).ocr_attachment,
        # Retroactive predicate normalization; trigger is the Phase-5 engine.
        "consolidate_predicates": Consolidator(maker).run,
    }
    try:
        await run_loop(maker, handlers)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
