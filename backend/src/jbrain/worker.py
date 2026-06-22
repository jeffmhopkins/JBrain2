"""Worker entrypoint: the job-queue loop.

Single-threaded by design at personal scale: claim one job, run it, repeat.
Startup backfill enqueues ingestion for every note still marked 'pending'
(migration 0003 stamps all pre-existing notes with it), so the index
self-heals after upgrades without manual intervention. The heartbeat log
line keeps the service honest in `docker compose ps` and the Ops screen.
"""

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jbrain import ops_metrics, queue
from jbrain.agent.correctionmine import CORRECTION_MINE_SPEC, correction_mine_handler
from jbrain.agent.predicatereview import PREDICATE_REVIEW_SPEC, predicate_review_handler
from jbrain.agent.promptselfedit import PROMPT_SELF_EDIT_SPEC, prompt_self_edit_handler
from jbrain.agent.skilldistill import SKILL_DISTILL_SPEC, skill_distill_handler
from jbrain.agent.skillsweep import SKILL_SWEEP_SPEC, skill_sweep_handler
from jbrain.analysis import purge
from jbrain.analysis.consolidation import Consolidator
from jbrain.analysis.hygiene import ENTITY_HYGIENE_SPEC, entity_hygiene_handler
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.reembed import REEMBED_SPEC, reembed_handler
from jbrain.analysis.tagconsolidate import TAG_CONSOLIDATE_SPEC, tag_consolidate_handler
from jbrain.config import get_settings
from jbrain.db.session import ScopeStampError, SessionContext, narrowed_context
from jbrain.embed import NoteEmbedder, PredicateEmbedder, TeiEmbedClient
from jbrain.ingest import ocr
from jbrain.ingest.ocr import OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.ingest.transcribe_job import TRANSCRIBE_ATTACHMENT_SPEC, TranscribePipeline
from jbrain.llm import build_router
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.schema import get_registry
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from jbrain.transcribe import WhisperCppClient
from jbrain.usage import SqlUsageRecorder
from jbrain.wiki.actions import WIKI_SPECS, wiki_handlers
from jbrain.wiki.rewriter import LlmRewriter
from jbrain.workflow import dispatcher, scheduler
from jbrain.workflow.eval_scorer import build_live_scorer, eval_run_handler
from jbrain.workflow.evalaction import EVAL_RUN_SPEC
from jbrain.workflow.registry import ACTION_SPECS, ActionRegistry, build_registry
from jbrain.workflow.runlog import PipelineRunLog

log = structlog.get_logger()

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 60
# Host-metrics sampling cadence (the owner's 30s choice) and the slower rollup +
# retention pass. Both ride the existing loop, like the scheduler tick.
METRICS_SAMPLE_SECONDS = 30
METRICS_MAINTENANCE_SECONDS = 300

# A handler runs one job. The six shipped kinds take only the payload (they manage
# their own SYSTEM_CTX internally — system pipelines that legitimately cross every
# domain), which is the type the action registry's dispatch table produces. An
# owner/agent-triggered handler may instead accept the resolved execution
# `SessionContext` as a second argument and run its queries under that *narrowed*
# scope (E1); the worker inspects arity (`_invoke`) and passes the context only when
# the handler asks for it, so the existing handlers are untouched.
Handler = Callable[[dict[str, Any]], Awaitable[None]]

# A handler that opts into the narrowed execution scope by accepting it explicitly.
ScopedHandler = Callable[[dict[str, Any], SessionContext], Awaitable[None]]


def resolve_exec_context(job: queue.Job) -> SessionContext:
    """The `SessionContext` a claimed job's handler runs under (E1, no confused
    deputy). An UNSTAMPED job (both stamp halves NULL — every job today and the six
    shipped kinds) runs under the all-domains `SYSTEM_CTX`, exactly as before. A
    STAMPED job narrows to its (principal_id, domain_code) scope. A *partial* stamp
    raises ScopeStampError in `narrowed_context` — fail-closed, never a silent
    widening to system (the caller fails the job)."""
    if not job.is_stamped:
        return queue.SYSTEM_CTX
    return narrowed_context(job.principal_id, job.domain_code)


async def _invoke(
    handler: Handler | ScopedHandler, payload: dict[str, Any], ctx: SessionContext
) -> None:
    """Call a handler, passing the resolved execution context only to a handler that
    declares a second parameter for it (the narrowed-scope handlers). The existing
    payload-only handlers are called exactly as before."""
    takes_ctx = len(inspect.signature(handler).parameters) >= 2
    if takes_ctx:
        await cast("ScopedHandler", handler)(payload, ctx)
    else:
        await cast("Handler", handler)(payload)


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
        # Resolve the execution scope BEFORE running the handler: a malformed
        # (partial) stamp must fail the job, never silently widen to SYSTEM_CTX.
        exec_ctx = resolve_exec_context(job)
    except ScopeStampError as exc:
        # Fail-closed: a half-stamped job is config drift / a smuggled escalation
        # attempt — fail it permanently rather than run it under any scope.
        await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc), permanent=True)
        log.error("worker.job_bad_scope_stamp", job_id=job.id, kind=job.kind, error=repr(exc))
        return True
    ran_as = "system" if exec_ctx is queue.SYSTEM_CTX else "scoped"
    try:
        await _invoke(handler, job.payload, exec_ctx)
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
        # ran_as records E1's scope choice (system vs scoped) on the audit line:
        # an owner-system run is visible as such, not a smuggled escalation.
        log.info("worker.job_done", job_id=job.id, kind=job.kind, ran_as=ran_as)
    return True


async def _after_exhaustion(
    maker: async_sessionmaker[AsyncSession], job: queue.Job, exhausted: bool
) -> None:
    """Kind-specific fallbacks once a job has burned its whole retry budget.

    An exhausted ocr_attachment or transcribe_attachment must not strand its note
    unanalyzed: the ingest gate deferred analysis to attachment work that will now
    never finish, so fall back to body-only analysis (jbrain.ingest.ocr's gate
    spans both backends).
    """
    if not exhausted or job.kind not in ("ocr_attachment", "transcribe_attachment"):
        return
    attachment_id = job.payload.get("attachment_id")
    if attachment_id is not None:
        await ocr.enqueue_analysis_fallback(maker, str(attachment_id))


async def _sample_metrics_safely(
    maker: async_sessionmaker[AsyncSession], client: httpx.AsyncClient, token: str
) -> None:
    """Store one host-metrics sample, swallowing any error so a supervisor blip
    (or a transient DB error) never disturbs the job loop — like the scheduler tick."""
    try:
        await ops_metrics.sample_once(maker, queue.SYSTEM_CTX, client, token)
    except Exception as exc:  # noqa: BLE001 - a missed sample must not kill the worker
        log.warning("worker.metrics_sample_error", error=repr(exc))


async def _maintain_metrics_safely(maker: async_sessionmaker[AsyncSession], *, boot: bool) -> None:
    """Refresh the hourly rollup and prune past-retention rows. The boot pass
    rolls up the full raw-retention window in case the worker was down for a
    while; steady-state passes only refresh the trailing few hours."""
    try:
        window = ops_metrics.RAW_RETENTION if boot else ops_metrics.ROLLUP_WINDOW
        await ops_metrics.rollup(maker, queue.SYSTEM_CTX, window=window)
        await ops_metrics.prune(maker, queue.SYSTEM_CTX)
    except Exception as exc:  # noqa: BLE001 - rollup/prune is best-effort maintenance
        log.warning("worker.metrics_maintain_error", error=repr(exc))


async def run_loop(
    maker: async_sessionmaker[AsyncSession],
    handlers: dict[str, Handler],
    registry: ActionRegistry | None = None,
    settings: SqlSettingsStore | None = None,
    *,
    supervisor_client: httpx.AsyncClient | None = None,
    supervisor_token: str = "",
) -> None:
    backfilled = False
    last_heartbeat = 0.0
    last_tick = 0.0
    last_dispatch = 0.0
    last_sample = 0.0
    last_maintenance = 0.0
    metrics_booted = False
    # The run-log writer the dispatcher uses when LIVE: one pipeline run per
    # dispatched event (§8). Built once off the same maker (owner-scoped writes).
    run_log = PipelineRunLog(maker)
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
        # Run the dispatcher tick alongside the scheduler tick: claim undispatched
        # events, diff the engine's would-be enqueue against the hardcoded path, and
        # mark them dispatched (workflow/dispatcher.py). In SHADOW (the prod default)
        # it never enqueues; in LIVE (the Wave-2 cutover, an operator settings flip)
        # it also enqueues + run-logs. Gated by `workflow_dispatch` +
        # `workflow_dispatch_mode` and fault-swallowed exactly like the scheduler
        # tick, so it can never disturb the live job loop.
        if (
            registry is not None
            and settings is not None
            and now - last_dispatch >= dispatcher.TICK_SECONDS
        ):
            await dispatcher.run_tick_safely(maker, registry, settings=settings, run_log=run_log)
            last_dispatch = now
        # Host-metrics telemetry rides the same loop (the worker is the singleton
        # background process, the right home for a periodic sampler). Gated on a
        # configured supervisor; both ticks are fault-swallowed like the others.
        if supervisor_client is not None and now - last_sample >= METRICS_SAMPLE_SECONDS:
            await _sample_metrics_safely(maker, supervisor_client, supervisor_token)
            last_sample = now
        if supervisor_client is not None and now - last_maintenance >= METRICS_MAINTENANCE_SECONDS:
            await _maintain_metrics_safely(maker, boot=not metrics_booted)
            metrics_booted = True
            last_maintenance = now
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
    # Audio transcription is gated on the whisper backend being configured: an
    # empty whisper_url leaves audio attachments un-enqueued (no chunks) rather
    # than queuing a transcribe_attachment job no model can serve.
    transcribe_enabled = bool(settings.whisper_url)
    pipeline = IngestPipeline(
        maker,
        blobs,
        transcribe_enabled=transcribe_enabled,
        transcribe_max_bytes=settings.whisper_max_bytes,
    )
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
        # The audio sibling: transcribe an audio attachment via the whisper model in
        # the local gateway, unloading it after (load-on-demand / unload-after). The
        # handler is always wired so the action registry pairs; when whisper is
        # unconfigured ingest enqueues no transcribe job, so it stays dormant.
        "transcribe_attachment": TranscribePipeline(
            maker,
            blobs,
            WhisperCppClient(
                settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
            ),
            settings.whisper_model,
            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,
        ).transcribe_attachment,
        # Retroactive predicate normalization; trigger is the Phase-5 engine.
        "consolidate_predicates": Consolidator(maker).run,
        # Keep the canonical_predicates index in step with the schema registry.
        "sync_predicates": predicate_embedder.sync_predicates,
        # The deleted-note-artifact purge as a fireable action (Phase-5 Track B):
        # the same boot sweep, now also runnable on a nightly schedule / on demand.
        "purge_deleted_artifacts": scheduler.purge_handler(maker),
        # The two boot self-heal backfills as fireable actions (Phase-5 Wave 2 —
        # the dropped-event safety net). They still run at boot below, AND now on a
        # recurring schedule + on-demand from Ops, so a dropped best-effort event
        # self-heals within minutes rather than at the next restart.
        "reconcile_pending_notes": scheduler.reconcile_pending_notes_handler(maker),
        "reconcile_pending_integration": scheduler.reconcile_pending_integration_handler(maker),
        # The unembedded-notes backfill, likewise promoted off boot-only (Track S):
        # a dropped embed_note enqueue no longer strands a note's chunks unembedded
        # until the next restart — it self-heals within minutes / on demand from Ops.
        "reconcile_unembedded_notes": scheduler.reconcile_unembedded_notes_handler(maker),
        # The geofence reconciler backstop (Phase 7 Wave 3c): the scheduled twin of
        # the inline detection at ingest. Rebuilds the place_geofence mirror from the
        # graph and re-evaluates each subject's latest fix, healing a dropped
        # projector hook or inline transition. In-code only (not the app.actions
        # seed); a migration seeds its schedule + pipeline. Runs as the full owner.
        "geofence_sweep": scheduler.geofence_sweep_handler(maker),
        # The opt-in self-improvement eval (Phase-5 Track H·A): runs the note.extract
        # suite through the LLM adapter behind the budget gate (fail-closed over the
        # kill-switch / daily token budget). The live scorer is built here off the
        # worker's router; CI never reaches it (no model). Like the purge/reconcile
        # actions it lives in-code only — NOT in the app.actions seed — so the 0035
        # seed-lockstep holds (the seed projection + nightly schedule are H·B).
        "eval_run": eval_run_handler(maker, build_live_scorer(router)),
        # Loop 2 skill distillation (Wave 2): distill successful runs into owner-reviewed shadow
        # skills, budget-gated. In-code only (not in app.actions); a migration seeds the schedule.
        "skill_distill": skill_distill_handler(
            maker,
            router=router,
            embedder=TeiEmbedClient(settings.embed_url),
            embedding_model=settings.embed_model,
        ),
        # Loop 2 skill hygiene (Wave 3): cap active skills per domain, demoting the least-useful
        # back to shadow (reversible). No LLM call; in-code only (a migration seeds the schedule).
        "skill_sweep": skill_sweep_handler(maker),
        # Loop 3a predicate-canon review (Wave 2): stage owner proposals to resolve open
        # new_predicate cards (map/mint). No LLM call; in-code only (a migration seeds it).
        "predicate_review": predicate_review_handler(maker),
        # Loop 3b Tier-B (correction mining): read ended chats for owner corrections of facts and
        # stage owner correction-note proposals (the LLM judges; budget-gated). In-code only.
        "correction_mine": correction_mine_handler(maker, router=router),
        # Loop 4 Wave 3 (prompt self-edit): draft prompt-edit proposals for prompts whose
        # proposals the owner keeps rejecting, budget-gated. In-code only (a migration seeds it,
        # disabled by default). Propose-only — never applies a change (#6).
        "prompt_self_edit": prompt_self_edit_handler(maker, router=router),
        # Phase-6 hygiene sweeps (docs/HYGIENE_SWEEPS_PLAN.md): core-data maintenance, no LLM,
        # in-code only (a migration seeds the schedules, disabled by default). entity_hygiene
        # deletes provisional orphan entities; reembed_stale re-embeds stale-model skills/entities
        # (local embed container); tag_consolidate folds drift tag spellings to canonical.
        "entity_hygiene": entity_hygiene_handler(maker),
        "reembed_stale": reembed_handler(
            maker, embedder=TeiEmbedClient(settings.embed_url), embedding_model=settings.embed_model
        ),
        "tag_consolidate": tag_consolidate_handler(maker),
        # The wiki builder (Phase-6 Wave C2): dirty-bit-driven article build + reindex + prune.
        # In-code only (not in the app.actions seed); a migration seeds the schedules. The live
        # LLM rewriter (C2b) drives router.complete behind the grounding gate + wiki-build budget;
        # CI fakes the router. (The deterministic StubRewriter remains the default for tests.)
        **wiki_handlers(
            maker,
            embed=TeiEmbedClient(settings.embed_url),
            embedding_model=settings.embed_model,
            rewriter=LlmRewriter(router, settings=worker_settings_store, ctx=queue.SYSTEM_CTX),
        ),
    }
    # Build the dispatch table from the action registry (W0.1): an action without
    # a handler — or a handler with no registered action — fails the worker LOUDLY
    # here at boot, like the schema registry above, rather than failing a job at run
    # time (the old "no handler for kind" path). Behavior for known kinds is
    # unchanged: the dispatch table is the same {kind: handler} map as before. The
    # registry adds the purge action, the three reconcilers, and the opt-in eval_run
    # to the shipped six (all in-code only, not in the app.actions seed — see
    # scheduler.PURGE_ACTION / RECONCILE_*_ACTION / EVAL_RUN_SPEC).
    registry = build_registry(
        (
            *ACTION_SPECS,
            scheduler.PURGE_ACTION,
            scheduler.RECONCILE_PENDING_NOTES_ACTION,
            scheduler.RECONCILE_PENDING_INTEGRATION_ACTION,
            scheduler.RECONCILE_UNEMBEDDED_NOTES_ACTION,
            scheduler.GEOFENCE_SWEEP_ACTION,
            TRANSCRIBE_ATTACHMENT_SPEC,
            EVAL_RUN_SPEC,
            SKILL_DISTILL_SPEC,
            SKILL_SWEEP_SPEC,
            PREDICATE_REVIEW_SPEC,
            CORRECTION_MINE_SPEC,
            PROMPT_SELF_EDIT_SPEC,
            ENTITY_HYGIENE_SPEC,
            REEMBED_SPEC,
            TAG_CONSOLIDATE_SPEC,
            *WIKI_SPECS,
        )
    )
    handlers = registry.dispatch_table(impls)
    # The host-metrics sampler reads the supervisor (the only container with the
    # host's /proc + /sys mounted) over the internal network. Gated on a token so
    # an unconfigured dev worker doesn't spin on 401s; the worker shares the api's
    # JBRAIN_SUPERVISOR_* env.
    supervisor_client = (
        httpx.AsyncClient(base_url=settings.supervisor_url, timeout=30.0)
        if settings.supervisor_token
        else None
    )
    try:
        # The shadow dispatcher reads its `workflow_dispatch` gate through the same
        # live settings store the LLM router uses, so the operator can silence it
        # without a redeploy.
        await run_loop(
            maker,
            handlers,
            registry,
            settings=worker_settings_store,
            supervisor_client=supervisor_client,
            supervisor_token=settings.supervisor_token,
        )
    finally:
        if supervisor_client is not None:
            await supervisor_client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
