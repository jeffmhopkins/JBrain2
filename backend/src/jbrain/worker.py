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
from jbrain.embed import NoteEmbedder, PredicateEmbedder, TeiEmbedClient
from jbrain.ingest import ocr
from jbrain.ingest.ocr import OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import build_router
from jbrain.schema import get_registry
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from jbrain.usage import SqlUsageRecorder
from jbrain.workflow import scheduler
from jbrain.workflow.registry import ACTION_SPECS, ActionRegistry, build_registry

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


async def run_loop(
    maker: async_sessionmaker[AsyncSession],
    handlers: dict[str, Handler],
    registry: ActionRegistry | None = None,
) -> None:
    backfilled = False
    last_heartbeat = 0.0
    last_tick = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            log.info("worker.heartbeat")
            last_heartbeat = now
        # Run the scheduler tick on its own cadence: claim due schedules and
        # enqueue their bound pipelines (workflow/scheduler.py). Cheap when idle
        # (a single indexed due-query) and rides the same loop as the job claim,
        # so a nightly sweep needs no separate timer process.
        if registry is not None and now - last_tick >= scheduler.TICK_SECONDS:
            await scheduler.run_tick_safely(maker, registry)
            last_tick = now
        try:
            if not backfilled:
                ingests = await queue.backfill_pending_notes(maker, queue.SYSTEM_CTX)
                embeds = await queue.backfill_unembedded_notes(maker, queue.SYSTEM_CTX)
                # Drain the un-integrated backlog (bounded, oldest-first) so notes
                # ingested before integrate_note shipped self-heal at boot.
                analyses = await queue.backfill_pending_integration(maker, queue.SYSTEM_CTX)
                # Notes deleted before the purge cascade shipped left orphaned
                # derived artifacts (incl. resolved review history quoting
                # their text); sweep them once per boot.
                purged = await purge.backfill_deleted_note_artifacts(maker)
                # Normalize predicate drift left by older prompt versions.
                consolidations = await queue.backfill_consolidate(maker, queue.SYSTEM_CTX)
                # Seed/refresh the canonical_predicates index from the registry.
                predicate_syncs = await queue.backfill_sync_predicates(maker, queue.SYSTEM_CTX)
                backfilled = True
                log.info(
                    "worker.backfill",
                    ingest_jobs=ingests,
                    embed_jobs=embeds,
                    analyze_jobs=analyses,
                    purged_notes=purged,
                    consolidate_jobs=consolidations,
                    predicate_sync_jobs=predicate_syncs,
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
    predicate_embedder = PredicateEmbedder(
        maker, TeiEmbedClient(settings.embed_url), settings.embed_model
    )
    # Live per-task routing/reasoning overrides apply to worker LLM calls too,
    # so the settings screen governs background analysis without a restart.
    worker_settings_store = SqlSettingsStore(maker)
    router = build_router(
        settings,
        recorder=SqlUsageRecorder(maker),
        overrides_loader=lambda: worker_settings_store.llm_task_overrides(queue.SYSTEM_CTX),
    )
    # The embed client also powers entity-resolution layer 2 (similarity);
    # without it the resolver still runs layers 1/2b/3.
    analyzer = AnalysisPipeline(
        maker,
        router,
        embedder=TeiEmbedClient(settings.embed_url),
        embed_model=settings.embed_model,
        # Reads the predicate_canonicalization + value_shape_enforce toggles
        # (Phase 3/4); both default ON, flip off live via a settings upsert.
        settings=SqlSettingsStore(maker),
    )
    # Eager-load the schema registry so a missing/malformed defs/ fails the
    # worker LOUDLY at startup — never mid-note, where the SchemaError would
    # otherwise re-bill the extraction call on every retry.
    get_registry()
    impls: dict[str, Handler] = {
        "ingest_note": pipeline.ingest_note,
        "embed_note": embedder.embed_note,
        "integrate_note": analyzer.integrate_note,
        # The vision handler reads the image-analysis mode setting per job.
        "ocr_attachment": OcrPipeline(maker, blobs, router, SqlSettingsStore(maker)).ocr_attachment,
        # Retroactive predicate normalization; trigger is the Phase-5 engine.
        "consolidate_predicates": Consolidator(maker).run,
        # Keep the canonical_predicates index in step with the schema registry.
        "sync_predicates": predicate_embedder.sync_predicates,
        # The deleted-note-artifact purge as a fireable action (Phase-5 Track B):
        # the same boot sweep, now also runnable on a nightly schedule / on demand.
        "purge_deleted_artifacts": scheduler.purge_handler(maker),
    }
    # Build the dispatch table from the action registry (W0.1): an action without
    # a handler — or a handler with no registered action — fails the worker LOUDLY
    # here at boot, like the schema registry above, rather than failing a job at run
    # time (the old "no handler for kind" path). Behavior for known kinds is
    # unchanged: the dispatch table is the same {kind: handler} map as before. The
    # registry adds the purge action to the shipped six (it lives in-code only, not
    # in the app.actions seed — see scheduler.PURGE_ACTION).
    registry = build_registry((*ACTION_SPECS, scheduler.PURGE_ACTION))
    handlers = registry.dispatch_table(impls)
    try:
        await run_loop(maker, handlers, registry)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
