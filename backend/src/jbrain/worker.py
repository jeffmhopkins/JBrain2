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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from jbrain import ops_metrics, queue
from jbrain.analysis import purge
from jbrain.analysis.consolidation import Consolidator
from jbrain.analysis.hygiene import ENTITY_HYGIENE_SPEC, entity_hygiene_handler
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.predicates import retire_open_new_predicate_cards
from jbrain.analysis.reembed import REEMBED_SPEC, reembed_handler
from jbrain.analysis.tagconsolidate import TAG_CONSOLIDATE_SPEC, tag_consolidate_handler
from jbrain.config import get_settings
from jbrain.db.session import ScopeStampError, SessionContext, narrowed_context
from jbrain.embed import (
    ExternalSourceEmbedder,
    NoteEmbedder,
    PredicateEmbedder,
    ResearchReportEmbedder,
    TeiEmbedClient,
)
from jbrain.external.corpus import EMBED_EXTERNAL_SOURCE_SPEC
from jbrain.external.report_titler import ResearchReportTitler
from jbrain.external.research_corpus import (
    EMBED_RESEARCH_REPORT_SPEC,
    TITLE_RESEARCH_REPORT_SPEC,
)
from jbrain.gmail.provider import GmailClientProvider
from jbrain.gmail.triage import TRIAGE_INBOX_SPEC, triage_inbox_handler
from jbrain.ingest import ocr
from jbrain.ingest.emr.import_handler import EMR_PARSE_SPEC, EmrImportPipeline
from jbrain.ingest.emr.intake_handler import EMR_IMPORT_SPEC, EmrIntakePipeline
from jbrain.ingest.ocr import OcrPipeline
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.ingest.stream_analysis import ANALYZE_STREAM_URL_SPEC, StreamAnalysisPipeline
from jbrain.ingest.transcribe_job import TRANSCRIBE_ATTACHMENT_SPEC, TranscribePipeline
from jbrain.ingest.video import VIDEO_ANALYSIS_SPEC, VideoPipeline
from jbrain.llm import build_router
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.log_capture import LogScope, configure_logging
from jbrain.schema import get_registry
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBlobStore
from jbrain.transcribe import WhisperCppClient
from jbrain.usage import SqlUsageRecorder, TokenScope
from jbrain.wiki.actions import WIKI_SPECS, wiki_handlers
from jbrain.wiki.lint import WIKI_LINT_SPEC, wiki_lint_handler
from jbrain.wiki.rewriter import LlmRewriter
from jbrain.workflow import dispatcher, scheduler
from jbrain.workflow.preconditions import RETRY_AFTER, Precondition, model_already_loaded
from jbrain.workflow.registry import ACTION_SPECS, ActionRegistry, build_registry
from jbrain.workflow.runlog import (
    PipelineRunLog,
    finalize_job_step,
    reap_idle_run,
    set_run_progress,
)

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
# the handler asks for it, so the existing handlers are untouched. The return is
# `object`: most handlers return None, but the housekeeping sweeps return a work count
# the worker reads to reap an idle (0-work) fire's run.
Handler = Callable[[dict[str, Any]], Awaitable[object]]

# A handler that opts into the narrowed execution scope by accepting it explicitly.
ScopedHandler = Callable[[dict[str, Any], SessionContext], Awaitable[object]]

# A live-progress sink a long-running handler may opt into (a keyword-only `progress`
# parameter): each call updates its run's `progress_note` for the Ops "Runs" screen.
ProgressFn = Callable[[str], Awaitable[None]]


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
    handler: Callable[..., Awaitable[object]],
    payload: dict[str, Any],
    ctx: SessionContext,
    progress: ProgressFn,
) -> object:
    """Call a handler, injecting only the extras it actually declares, and return its
    result. The execution context is passed positionally to a handler with a second
    positional parameter (the narrowed-scope handlers); a `progress` callback is passed
    by keyword to a handler that declares a `progress` parameter (a long sweep reporting
    its progress). A plain payload-only handler is still called exactly as before. The
    return value is the handler's own — most return None; the housekeeping sweeps return
    a work count the caller uses to reap an idle (0-work) fire's run."""
    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    if "progress" in params:
        kwargs["progress"] = progress
    positional = sum(
        1
        for name, p in params.items()
        if name != "progress"
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    if positional >= 2:
        return await cast("ScopedHandler", handler)(payload, ctx, **kwargs)
    return await cast("Handler", handler)(payload, **kwargs)


async def process_one(
    maker: async_sessionmaker[AsyncSession],
    handlers: dict[str, Handler],
    *,
    registry: ActionRegistry | None = None,
    preconditions: Mapping[str, Precondition] | None = None,
) -> bool:
    """Claim and run a single job; returns False when the queue is idle.

    `registry` + `preconditions` enable the engine's precondition gate: an action that
    declares a `precondition` is checked before it runs, and a job whose precondition
    isn't met is DEFERRED (a fixed retry, no attempt burned) rather than run. Both
    default None — without them (every existing test, and any caller that doesn't wire
    a registry) a job runs exactly as before."""
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

    async def report_progress(note: str) -> None:
        # Live "processed X of Y" on this job's run, for the Ops "Runs" screen.
        # Best-effort: a progress write must never disturb the job it annotates.
        try:
            await set_run_progress(maker, queue.SYSTEM_CTX, job.id, note)
        except Exception:  # noqa: BLE001 — progress is an annotation, never the job's gate
            log.warning("worker.progress_write_failed", job_id=job.id)

    # The precondition gate (engine feature): if this action declares a precondition
    # and it isn't met, defer the job (fixed retry, no attempt burned) instead of
    # running it — so e.g. inbox triage waits for its local model to be resident rather
    # than forcing a swap. Skipped entirely when no registry/preconditions are wired.
    if await _deferred_on_precondition(maker, job, registry, preconditions, report_progress):
        return True

    # Tally the LLM tokens AND capture the structured-log trace this job emits, so its
    # run-log step shows the real cost + duration + a reviewable "full logs" view —
    # not the 0-token placeholder. logs.events is the tap; toks.total the token sum.
    with TokenScope() as toks, LogScope() as logs:
        try:
            result = await _invoke(handler, job.payload, exec_ctx, report_progress)
        except queue.PermanentJobError as exc:
            # Retrying cannot help (e.g. malformed extraction after the re-ask):
            # fail now instead of burning the retry budget.
            exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc), permanent=True)
            log.error("worker.job_failed_permanent", job_id=job.id, kind=job.kind, error=repr(exc))
            await _finalize_run_step(maker, job.id, ok=False, toks=toks, logs=logs)
            await _after_exhaustion(maker, job, exhausted)
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc))
            log.warning("worker.job_failed", job_id=job.id, kind=job.kind, error=repr(exc))
            # Only finalize when the job is truly done retrying; a re-queued job's
            # step stays open until its terminal attempt.
            if exhausted:
                await _finalize_run_step(maker, job.id, ok=False, toks=toks, logs=logs)
            await _after_exhaustion(maker, job, exhausted)
        else:
            await queue.complete(maker, queue.SYSTEM_CTX, job.id)
            # ran_as records E1's scope choice (system vs scoped) on the audit line:
            # an owner-system run is visible as such, not a smuggled escalation.
            log.info("worker.job_done", job_id=job.id, kind=job.kind, ran_as=ran_as)
            await _finalize_run_step(maker, job.id, ok=True, toks=toks, logs=logs)
            # A reap-eligible housekeeping sweep that reconciled nothing (count 0)
            # leaves a 0-work run behind; drop it so idle fires don't flood the Ops
            # run log. Only these sweeps return an int count, so `result == 0` never
            # matches an ordinary handler (which returns None).
            if job.kind in scheduler.REAPABLE_IDLE_SWEEPS and result == 0:
                await _reap_idle_run(maker, job.id)
    return True


async def _deferred_on_precondition(
    maker: async_sessionmaker[AsyncSession],
    job: queue.Job,
    registry: ActionRegistry | None,
    preconditions: Mapping[str, Precondition] | None,
    report_progress: ProgressFn,
) -> bool:
    """Evaluate the action's precondition (if any) and defer the job when it isn't met.

    Returns True when the job was deferred (the caller should stop and move on), False
    when it should run — including the common cases of no registry/map wired, an action
    with no precondition, or an unmet name with no registered check. A deferred job is
    rescheduled a fixed `RETRY_AFTER` out without burning an attempt, so it can wait
    indefinitely for the condition; its run step is left open (not finalized), exactly
    like a re-queued retry."""
    if registry is None or not preconditions:
        return False
    spec = registry.spec_for_handler(job.kind)
    if spec is None or spec.precondition is None:
        return False
    check = preconditions.get(spec.precondition)
    if check is None:
        return False
    result = await check()
    if result.met:
        return False
    await report_progress(f"deferred: {result.reason}")
    await queue.defer(maker, queue.SYSTEM_CTX, job.id, RETRY_AFTER, reason=result.reason)
    log.info("worker.job_deferred", job_id=job.id, kind=job.kind, reason=result.reason)
    return True


async def _finalize_run_step(
    maker: async_sessionmaker[AsyncSession],
    job_id: str,
    *,
    ok: bool,
    toks: TokenScope,
    logs: LogScope,
) -> None:
    """Stamp the job's outcome + token cost + captured log trace on its dispatched-run
    step (best-effort): a run-log write must never fail the executor job it annotates."""
    try:
        await finalize_job_step(
            maker,
            queue.SYSTEM_CTX,
            job_id,
            ok=ok,
            cost_tokens=toks.total,
            detail=logs.events,
        )
    except Exception:  # noqa: BLE001 — the run-log is an annotation, never the job's gate
        log.warning("worker.run_step_finalize_failed", job_id=job_id)


async def _reap_idle_run(maker: async_sessionmaker[AsyncSession], job_id: str) -> None:
    """Drop the 0-work run an idle housekeeping sweep opened (best-effort): reaping the
    run-log annotation must never fail the executor job that produced it."""
    try:
        await reap_idle_run(maker, queue.SYSTEM_CTX, job_id)
    except Exception:  # noqa: BLE001 — the run-log is an annotation, never the job's gate
        log.warning("worker.reap_idle_run_failed", job_id=job_id)


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
    maker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    token: str,
    tracker: ops_metrics.RateTracker,
) -> None:
    """Store one host-metrics sample, swallowing any error so a supervisor blip
    (or a transient DB error) never disturbs the job loop — like the scheduler tick.
    The `tracker` (one per worker) turns the supervisor's cumulative byte counters
    into the network/disk throughput rates the sample stores."""
    try:
        await ops_metrics.sample_once(maker, queue.SYSTEM_CTX, client, token, tracker=tracker)
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
    preconditions: Mapping[str, Precondition] | None = None,
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
    # One tracker for the loop's lifetime: it remembers the previous sample's byte
    # counters so each sample can store a network/disk throughput rate.
    rate_tracker = ops_metrics.RateTracker()
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
            await _sample_metrics_safely(maker, supervisor_client, supervisor_token, rate_tracker)
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
                # Retire the open new_predicate backlog the two-tier cutover
                # orphaned. Genuinely one-shot per database (a persisted
                # app.settings marker): reopen_review returns cards to 'open',
                # so a per-boot re-run would delete owner-reopened cards.
                retired_cards = await retire_open_new_predicate_cards(maker)
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
                    retired_predicate_cards=retired_cards,
                    predicate_sync_jobs=predicate_syncs,
                )
            if await process_one(maker, handlers, registry=registry, preconditions=preconditions):
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - survive DB blips, like the old heartbeat
            log.warning("worker.loop_error", error=repr(exc))
        await asyncio.sleep(POLL_SECONDS)


async def run() -> None:
    # Install the capture-tapped log chain so each job's structured-log trace lands
    # on its run step (the Runs "full logs" view); a no-scope job logs as normal.
    configure_logging()
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
    external_embedder = ExternalSourceEmbedder(
        maker, TeiEmbedClient(settings.embed_url), settings.embed_model
    )
    research_report_embedder = ResearchReportEmbedder(
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
    # The report display-title job (external.report_titler): one LLM one-shot per
    # report, so it takes the router rather than the embed container.
    research_report_titler = ResearchReportTitler(maker, router)
    # The embed client also powers entity-resolution layer 2 (similarity);
    # without it the resolver still runs layers 1/2b/3.
    analyzer = AnalysisPipeline(
        maker,
        router,
        embedder=TeiEmbedClient(settings.embed_url),
        embed_model=settings.embed_model,
        # Reads the value_shape_enforce toggle and the predicate-suggestion
        # picker toggle; both default ON, flip off live via a settings upsert.
        settings=SqlSettingsStore(maker),
    )
    # The archivist's Gmail provider, reading OAuth credentials live from the same
    # settings store the API uses (env fallback). The triage_inbox sweep holds the
    # bound `client` method, so a saved credential change takes effect with no restart;
    # until a refresh token exists the sweep fails with a recoverable "connect Gmail"
    # error and retries (docs/archive/EMAIL_ARCHIVIST_PLAN.md).
    gmail_provider = GmailClientProvider(
        worker_settings_store,
        settings,
        base_url=settings.gmail_api_url,
        token_url=settings.gmail_token_url,
    )
    # Eager-load the schema registry so a missing/malformed defs/ fails the
    # worker LOUDLY at startup — never mid-note, where the SchemaError would
    # otherwise re-bill the extraction call on every retry.
    get_registry()
    impls: dict[str, Handler] = {
        "ingest_note": pipeline.ingest_note,
        "embed_note": embedder.embed_note,
        "embed_external_source": external_embedder.embed_external_source,
        "embed_research_report": research_report_embedder.embed_research_report,
        "title_research_report": research_report_titler.title_research_report,
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
        # The video sibling (docs/archive/VIDEO_ANALYSIS_PLAN.md): sample + caption frames via
        # the vision route, transcribe the audio via the same whisper path (degrades
        # to frames-only when whisper is off), fuse on a timeline, and summarize.
        # On-demand only (the analyze_video tool kicks it, Wave 3), so it stays dormant
        # until asked; the handler is always wired so the action registry pairs.
        "analyze_video_attachment": VideoPipeline(
            maker,
            blobs,
            router,
            transcribe=(
                WhisperCppClient(
                    settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
                )
                if transcribe_enabled
                else None
            ),
            transcribe_model=settings.whisper_model,
            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,
        ).analyze_video_attachment,
        # The URL sibling of analyze_video_attachment, deferred off a chat turn
        # (DEFERRED_TOOL_CALLS_PLAN.md P2): resolve a video URL with yt-dlp, run the shared
        # stream pipeline, stream progress onto the result row for the task_status card,
        # and store the finished analysis. Kicked on demand by a deferred analyze_stream.
        "analyze_stream_url": StreamAnalysisPipeline(
            maker,
            blobs,
            router,
            transcribe=(
                WhisperCppClient(
                    settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
                )
                if transcribe_enabled
                else None
            ),
            transcribe_model=settings.whisper_model,
            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,
        ).analyze_stream_url,
        # EMR import (docs/plans/EMR_IMPORT_PLAN.md), a two-stage pipeline the seeded
        # `emr_import`/`emr_parse` triggers drive off note.ingested. Stage 1 decrypts the
        # archive in place and re-ingests (enqueue_ingest re-chunks the decrypted PDFs);
        # stage 2 parses those PDFs into cited facts via the shipped arbiter (no LLM).
        "emr_import": EmrIntakePipeline(
            maker,
            blobs,
            enqueue_ingest=lambda nid: queue.enqueue(
                maker, queue.SYSTEM_CTX, "ingest_note", {"note_id": nid}
            ),
        ).intake,
        "emr_parse": EmrImportPipeline(maker, blobs, analyzer).parse,
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
        # Phase-6 hygiene sweeps (docs/archive/HYGIENE_SWEEPS_PLAN.md): core-data
        # maintenance, no LLM,
        # in-code only (a migration seeds the schedules, disabled by default). entity_hygiene
        # deletes provisional orphan entities; reembed_stale re-embeds stale-model entities
        # (local embed container); tag_consolidate folds drift tag spellings to canonical.
        "entity_hygiene": entity_hygiene_handler(maker),
        "reembed_stale": reembed_handler(
            maker, embedder=TeiEmbedClient(settings.embed_url), embedding_model=settings.embed_model
        ),
        "tag_consolidate": tag_consolidate_handler(maker),
        # The archivist's inbox-triage sweep (docs/archive/EMAIL_ARCHIVIST_PLAN.md): classify
        # untriaged inbox mail into triaged/* priority labels, archiving all but `high`
        # (which stays in the inbox). The Gmail mechanics are direct API calls; only the
        # per-message classification is an LLM call (the `triage.classify` route). In-code
        # only (not in the app.actions seed); a migration seeds the schedule. Dormant until
        # Gmail is connected — the handler fails recoverably and retries until then.
        "triage_inbox": triage_inbox_handler(gmail_provider.client, router, maker),
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
        # The wiki health sweep (Phase-6 follow-on, docs/plans/WIKI_LINT_PLAN.md) — deterministic
        # checks (Wave A) + the LLM contradiction/stale-claim verifier (Wave B, metered against the
        # SEPARATE wiki-lint budget, router faked in CI). In-code only, not in the app.actions seed;
        # a migration seeds its schedule (disabled) + manual trigger. Standalone from the four
        # builder actions.
        "wiki_lint": wiki_lint_handler(
            maker,
            embedding_model=settings.embed_model,
            router=router,
            settings=worker_settings_store,
        ),
    }
    # Build the dispatch table from the action registry (W0.1): an action without
    # a handler — or a handler with no registered action — fails the worker LOUDLY
    # here at boot, like the schema registry above, rather than failing a job at run
    # time (the old "no handler for kind" path). Behavior for known kinds is
    # unchanged: the dispatch table is the same {kind: handler} map as before. The
    # registry adds the purge action and the three reconcilers to the shipped six
    # (all in-code only, not in the app.actions seed — see scheduler.PURGE_ACTION /
    # RECONCILE_*_ACTION).
    registry = build_registry(
        (
            *ACTION_SPECS,
            scheduler.PURGE_ACTION,
            scheduler.RECONCILE_PENDING_NOTES_ACTION,
            scheduler.RECONCILE_PENDING_INTEGRATION_ACTION,
            scheduler.RECONCILE_UNEMBEDDED_NOTES_ACTION,
            scheduler.GEOFENCE_SWEEP_ACTION,
            TRANSCRIBE_ATTACHMENT_SPEC,
            VIDEO_ANALYSIS_SPEC,
            ANALYZE_STREAM_URL_SPEC,
            EMBED_EXTERNAL_SOURCE_SPEC,
            EMBED_RESEARCH_REPORT_SPEC,
            TITLE_RESEARCH_REPORT_SPEC,
            ENTITY_HYGIENE_SPEC,
            REEMBED_SPEC,
            TAG_CONSOLIDATE_SPEC,
            TRIAGE_INBOX_SPEC,
            *WIKI_SPECS,
            WIKI_LINT_SPEC,
            EMR_IMPORT_SPEC,
            EMR_PARSE_SPEC,
        )
    )
    handlers = registry.dispatch_table(impls)
    # Action preconditions (workflow.preconditions): named gates the worker evaluates
    # before running a job. `reasoning_model_loaded` keeps the inbox-triage sweep from
    # forcing a local model swap — it runs only when the model `triage.classify`
    # resolves to is already resident (a cloud route, or local hosting off, is always
    # met). The route is resolved the same way the triage handler resolves it (the task
    # route + live override, no strength tier), so the gate matches what would actually
    # run. The gateway admin client points at the LLM gateway (not whisper's).
    preconditions: dict[str, Precondition] = {
        "reasoning_model_loaded": model_already_loaded(
            router, LocalGatewayClient(settings.local_llm_url), task="triage.classify"
        ),
    }
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
            preconditions=preconditions,
            supervisor_client=supervisor_client,
            supervisor_token=settings.supervisor_token,
        )
    finally:
        if supervisor_client is not None:
            await supervisor_client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
