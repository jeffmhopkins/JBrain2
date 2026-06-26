import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.gmailtools import build_gmail_handlers
from jbrain.agent.imagegentools import build_image_handlers
from jbrain.agent.loop import ToolHandler
from jbrain.agent.memory import MemoryRepo, MemoryService
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.readtools import build_registry
from jbrain.agent.runlog import AgentRunLog, RunLogReader
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcribetools import build_transcribe_handlers
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.agent.videotools import build_video_handlers
from jbrain.agent.webtools import build_web_handlers
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.analysis.hygiene import ENTITY_HYGIENE_SPEC
from jbrain.analysis.reembed import REEMBED_SPEC
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.analysis.tagconsolidate import TAG_CONSOLIDATE_SPEC
from jbrain.api import (
    agent,
    analysis,
    auth,
    chat_attachments,
    debug,
    debug_tokens,
    devices,
    family,
    feed,
    health,
    images,
    images_render,
    jcode,
    jcode_share,
    jcode_terminal,
    live,
    locations,
    member,
    mqtt,
    notes,
    ops,
    owntracks,
    pairing,
    proposals,
    runs,
    search,
    session_bridge,
    sessions,
    tiles,
    wiki,
)
from jbrain.api import (
    appointments as appointments_api,
)
from jbrain.api import gmail_settings as gmail_settings_api
from jbrain.api import image_settings as image_settings_api
from jbrain.api import lists as lists_api
from jbrain.api import llm_settings as llm_settings_api
from jbrain.api import settings as settings_api
from jbrain.api import (
    tasks as tasks_api,
)
from jbrain.api.debug_activity import DebugActivity
from jbrain.appointments.repo import SqlAppointmentsRepo
from jbrain.auth.repo import SqlAuthRepo
from jbrain.citygeocode import CityGeocoder
from jbrain.config import Settings, get_settings
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.geocoding import geocode_connectors
from jbrain.connectors.medical import medical_connectors
from jbrain.connectors.repo import SqlConnectorCache
from jbrain.connectors.service import ConnectorService
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.embed import TeiEmbedClient
from jbrain.family import SqlFamilyRepo
from jbrain.geocode import NominatimReverseClient
from jbrain.gmail import GmailClientProvider
from jbrain.gmail.triage import TRIAGE_INBOX_SPEC
from jbrain.image_gen.comfyui import ComfyUiImageGen
from jbrain.image_gen.gateway import ComfyUiGatewayClient
from jbrain.image_gen.render import ImageRenderService
from jbrain.jcode import JcodeClient
from jbrain.lists.repo import SqlListsRepo
from jbrain.llm import build_router
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.locations import SqlLocationRepo
from jbrain.locations.live import LiveBroadcaster, live_feeder
from jbrain.locations.pairing import SqlPairingRepo
from jbrain.locations.ratelimit import TokenBucket
from jbrain.locations.viewscope import SqlViewScopeRepo
from jbrain.media import ffmpeg_available
from jbrain.models.images import GeneratedImageRepo
from jbrain.notes.repo import SqlNotesRepo
from jbrain.push import SqlFcmTokenRepo
from jbrain.queue import SYSTEM_CTX, PgJobQueue
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBackupShelf, FsBlobStore
from jbrain.tasks.repo import TaskRepo, TaskRunRepo
from jbrain.tasks.runner import LoopTurnExecutor, TaskRunner
from jbrain.tasks.scheduler import run_tasks_loop
from jbrain.tiles import FsTileCache, HttpTileFetcher, TileService, TileSet, tile_cache_namespace
from jbrain.transcribe import WhisperCppClient
from jbrain.usage import SqlUsageRecorder
from jbrain.web import SearxngClient, WebFetcher
from jbrain.wiki.actions import WIKI_SPECS
from jbrain.wiki.readstore import WikiReadStore
from jbrain.wiki.talkstore import WikiTalkStore
from jbrain.workflow.automations import AutomationsReader
from jbrain.workflow.registry import ACTION_SPECS
from jbrain.workflow.registry import build_registry as build_action_registry
from jbrain.workflow.scheduler import (
    GEOFENCE_SWEEP_ACTION,
    PURGE_ACTION,
    RECONCILE_PENDING_INTEGRATION_ACTION,
    RECONCILE_PENDING_NOTES_ACTION,
    RECONCILE_UNEMBEDDED_NOTES_ACTION,
)

structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
)

# The action specs the API's registry carries: the shipped six plus every in-code
# action the worker can dispatch that the Ops surface must resolve — the purge sweep,
# the three reconcilers, the geofence sweep, the Phase-6 hygiene sweeps, the wiki
# builder, and the archivist's inbox triage. An action with a seeded manual trigger
# MUST be here, or `fire_trigger` -> `registry.get` raises and "Run now" fails; the
# Catalog/Automations surface renders this set. (Dispatch-only actions with no Ops
# trigger — transcribe/video — live only in the worker's registry.) A module constant
# so the seeded-trigger lockstep is unit-testable (test_main_registry).
API_ACTION_SPECS = (
    *ACTION_SPECS,
    PURGE_ACTION,
    RECONCILE_PENDING_NOTES_ACTION,
    RECONCILE_PENDING_INTEGRATION_ACTION,
    RECONCILE_UNEMBEDDED_NOTES_ACTION,
    GEOFENCE_SWEEP_ACTION,
    ENTITY_HYGIENE_SPEC,
    REEMBED_SPEC,
    TAG_CONSOLIDATE_SPEC,
    *WIKI_SPECS,
    TRIAGE_INBOX_SPEC,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.engine = engine
        app.state.session_maker = maker
        # In-flight chat turns, detached from their SSE response so a backgrounded PWA
        # can't kill them; keyed by run_id for the Stop endpoint and shutdown cleanup.
        app.state.live_turns = {}
        app.state.auth_repo = SqlAuthRepo(maker)
        app.state.device_repo = SqlDeviceRepo(maker)
        app.state.location_repo = SqlLocationRepo(maker)
        app.state.view_scope_repo = SqlViewScopeRepo(maker)
        app.state.pairing_repo = SqlPairingRepo(maker)
        app.state.fcm_token_repo = SqlFcmTokenRepo(maker)
        app.state.family_repo = SqlFamilyRepo(maker)
        # The content-free poke notifier (M6). None until a Firebase project +
        # service-account credentials are configured (the FcmNotifier + its OAuth
        # token provider are wired at deploy / with the Android receiver); a None
        # notifier makes every crossing's poke a no-op.
        app.state.push_notifier = None
        # Anti-brute-force on the unauthenticated redeem endpoint: ~10 attempts
        # burst per source IP, refilling 1 every 10s.
        app.state.pairing_rate_limiter = TokenBucket(capacity=10, refill_per_sec=0.1)
        # The live feed's in-process fan-out, fed by an MQTT subscriber that runs
        # only when the ingest identity is configured (same gate as the M1 consumer)
        # — so a stock deploy / the tests never open a broker connection.
        app.state.live_broadcaster = LiveBroadcaster()
        live_task: asyncio.Task[None] | None = None
        if settings.mqtt_ingest_secret:
            live_task = asyncio.create_task(
                live_feeder(settings, app.state.auth_repo, app.state.live_broadcaster)
            )
        # Server-side basemap tile proxy/cache: the map's Leaflet layer fetches
        # tiles only from this box (api/tiles.py); the upstream is fetched once and
        # cached. One independent service per selectable scheme (dark/light), each
        # cache-namespaced by its upstream URL so a style change — or the app's
        # light/dark toggle — re-fetches cleanly instead of serving the old style's
        # cached z/x/y tiles. Empty upstream disables that scheme (map falls back to
        # the on-box schematic).
        fetcher = HttpTileFetcher(settings.tile_user_agent)

        def _scheme(upstream: str) -> TileService:
            return TileService(
                FsTileCache(Path(settings.tile_cache_dir) / tile_cache_namespace(upstream)),
                fetcher,
                upstream_template=upstream,
                max_zoom=settings.tile_max_zoom,
            )

        app.state.tile_set = TileSet(
            {
                "dark": _scheme(settings.tile_upstream_url),
                "light": _scheme(settings.tile_upstream_url_light),
            },
            default=settings.tile_default_scheme,
        )
        # Per-device ingest cap: 60 fixes/min sustained, burst 120 so a batched
        # offline backfill (up to MAX_BATCH=100 fixes in one POST) is accepted at
        # once. A flooding device still 429s and backs off; one token per fix.
        app.state.location_rate_limiter = TokenBucket(capacity=120, refill_per_sec=1.0)
        app.state.notes_repo = SqlNotesRepo(maker)
        app.state.lists_repo = SqlListsRepo(maker)
        app.state.appointments_repo = SqlAppointmentsRepo(maker)
        app.state.blob_store = FsBlobStore(settings.blob_dir)
        app.state.generated_image_repo = GeneratedImageRepo()
        app.state.backup_shelf = FsBackupShelf(settings.backups_dir)
        app.state.job_queue = PgJobQueue(maker)
        # The action registry the emergency-trigger control resolves a sweep's
        # pipeline through (workflow/scheduler.fire_trigger) and the Automations
        # surface renders the Catalog from. Composed from API_ACTION_SPECS (module
        # scope, so the seeded-trigger lockstep is unit-tested) — every action with a
        # seeded manual trigger must be in it, or "Run now" raises ActionRegistryError.
        action_registry = build_action_registry(API_ACTION_SPECS)
        app.state.action_registry = action_registry
        app.state.search_service = SearchService(
            SqlSearchRepo(maker), TeiEmbedClient(settings.embed_url)
        )
        app.state.analysis_repo = SqlAnalysisRepo(maker)
        app.state.wiki_read_store = WikiReadStore(maker)
        app.state.wiki_talk_store = WikiTalkStore(maker)
        # Shared embedder for read-side embedding lookups (the review predicate
        # picker's on-demand suggestions).
        app.state.embed_client = TeiEmbedClient(settings.embed_url)
        settings_store = SqlSettingsStore(maker)
        app.state.settings_store = settings_store
        # Admin client for the local-model gateway (runtime loaded-state + unload).
        # Best-effort; the settings screen tolerates it being unreachable.
        app.state.local_gateway = LocalGatewayClient(settings.local_llm_url)
        # Any API-side LLM call must flow through this router so its tokens
        # land in app.llm_usage like the worker's do. The overrides loader reads
        # the live per-task routing/reasoning settings (SYSTEM_CTX owner session)
        # on each call so the settings screen takes effect without a restart.
        app.state.llm_router = build_router(
            settings,
            recorder=SqlUsageRecorder(maker),
            overrides_loader=lambda: settings_store.llm_task_overrides(SYSTEM_CTX),
            local_windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
        )
        # The agent: Tier-A memory, the tool registry (validated against the .tool
        # sidecars at startup), the session capability store, and the run log.
        app.state.agent_memory = MemoryService(
            MemoryRepo(maker), TeiEmbedClient(settings.embed_url), settings.embed_model
        )
        app.state.agent_proposals = ProposalRepo(maker)
        # The egress chokepoint: a fixed allowlist of connectors, served only on an
        # approved egress Proposal (invariant #9).
        connector_registry = ConnectorRegistry(
            [
                *medical_connectors(settings.rxnav_url, settings.medlineplus_url),
                *geocode_connectors(settings.external_geocoder_url),
            ]
        )
        app.state.connector_service = ConnectorService(connector_registry, SqlConnectorCache(maker))
        # The jerv chatbot's on-box internet tools — direct, sandboxed web access
        # (no owner data in context; docs/ASSISTANT.md "Agent selection").
        web_handlers = build_web_handlers(SearxngClient(settings.searxng_url), WebFetcher())
        # The archivist persona's Gmail tools. Always wired over a provider that reads
        # the OAuth credentials live from the settings panel (env fallback), so a saved
        # change takes effect with no restart; until a refresh token exists the tools
        # report "connect Gmail in Settings" (docs/EMAIL_ARCHIVIST_PLAN.md).
        app.state.gmail_provider = GmailClientProvider(
            settings_store,
            settings,
            base_url=settings.gmail_api_url,
            token_url=settings.gmail_token_url,
        )
        gmail_handlers = build_gmail_handlers(app.state.gmail_provider.client)
        # The on-box geocoder: an offline nearest-city reverse lookup (no resident
        # service, no RAM at rest, no egress) shared by the curator's geocode_reverse,
        # the map's reverse-geocode endpoint, and jerv's current_location. The
        # owner-configured external geocoder is the direct street-address fallback for
        # jerv (default off when external_geocoder_url is unset).
        app.state.city_geocoder = CityGeocoder()
        external_reverse = NominatimReverseClient(settings.external_geocoder_url)
        # Built before the registry: edit_image resolves a chat attachment's bytes
        # through the same TurnAttachmentRepo, so it must exist first.
        app.state.agent_sessions = AgentSessionRepo(maker)
        app.state.turn_attachments = TurnAttachmentRepo(maker, app.state.agent_sessions)
        # jerv's local image generator (docs/IMAGE_GEN_PLAN.md). Wired only when a
        # host-managed ComfyUI is configured; None otherwise, so an unconfigured box
        # silently lacks the feature — the registry then drops the image sidecars. The
        # client is dedicated because ComfyUI's long generations want their own timeout
        # budget, set inside ComfyUiImageGen.
        image_gen_client: httpx.AsyncClient | None = None
        image_handlers: dict[str, ToolHandler] = {}
        if settings.comfyui_url:
            image_gen_client = httpx.AsyncClient()
            app.state.image_gen = ComfyUiImageGen(
                settings.comfyui_url, image_gen_client, timeout=settings.comfyui_timeout
            )
            # The management client (status/free) for the owner image-settings surface
            # — the sibling of app.state.local_gateway, wired on the same gate.
            app.state.comfyui_gateway = ComfyUiGatewayClient(settings.comfyui_url)
            # The shared render core (Wave L2): the jerv handlers AND the direct owner API
            # (api/images_render) drive this one path, so behavior never diverges. It owns the
            # unified-memory time-share (free the LLM before / ComfyUI after a render), the
            # blob put, and the RLS-scoped row insert.
            app.state.image_render = ImageRenderService(
                app.state.image_gen,
                app.state.blob_store,
                app.state.generated_image_repo,
                maker,
                app.state.local_gateway,
                app.state.comfyui_gateway,
                settings.comfyui_models,
            )
            image_handlers = build_image_handlers(
                app.state.image_gen,
                app.state.blob_store,
                app.state.generated_image_repo,
                app.state.turn_attachments,
                maker,
                # The image render frees any resident local LLM first (unified-memory
                # time-share); llama-swap reloads it on the loop's next call.
                app.state.local_gateway,
                # …and frees ComfyUI's resident diffusion model AFTER the render, so the
                # ~39 GB it pins returns to the pool for the reply's LLM reload, a
                # follow-up edit, or switching back to a large local model.
                app.state.comfyui_gateway,
                # Routes analyze_image's vision read (the `agent.vision` task) so a
                # text-only agent model can still see an image via a vision model.
                app.state.llm_router,
                # The provisioned catalog ids gate the `speed: fast` path — a fast request
                # for a model the operator never installed fails with a clear, actionable
                # message rather than ComfyUI's opaque missing-checkpoint error.
                settings.comfyui_models,
                # The handlers are a thin adapter over the shared service.
                render=app.state.image_render,
            )
        else:
            app.state.image_gen = None
            app.state.comfyui_gateway = None
            app.state.image_render = None
        # jerv's on-box audio transcription (docs/WHISPER_TRANSCRIPTION_PLAN.md).
        # Wired only when the whisper gateway is configured; the registry drops the
        # `transcribe` sidecar otherwise (graceful degrade, like the image tools).
        # The gateway frees the model after each call (load-on-demand / unload-after).
        transcribe_handlers: dict[str, ToolHandler] = {}
        if settings.whisper_url:
            transcribe_handlers = build_transcribe_handlers(
                WhisperCppClient(
                    settings.whisper_url,
                    settings.whisper_model,
                    timeout=settings.whisper_timeout,
                ),
                app.state.blob_store,
                app.state.turn_attachments,
                settings.whisper_model,
                gateway=LocalGatewayClient(settings.whisper_url),
                max_bytes=settings.whisper_max_bytes,
            )
        # Code mode (docs/proposed/JCODE_PLAN.md, Wave J2): the api proxies an owner's
        # sandboxed coding session to the internal jcode control server. Wired only when
        # configured — the owner-gated routes 404 otherwise (graceful degrade). The turn
        # registry holds in-flight SSE turns for reconnect/cancel.
        app.state.jcode_client = (
            JcodeClient(settings.jcode_url, settings.jcode_token) if settings.jcode_url else None
        )
        app.state.jcode_turns = {}
        # jerv's on-box video analysis (docs/VIDEO_ANALYSIS_PLAN.md): sample + caption
        # frames and transcribe the audio inline, like analyze_image/transcribe. Wired
        # only when ffmpeg can sample frames, so a box without it silently lacks the
        # feature (the registry drops the `analyze_video` sidecar, graceful degrade like
        # the image/whisper tools). Whisper is optional — frames-only without it.
        video_handlers: dict[str, ToolHandler] = {}
        if ffmpeg_available():
            video_handlers = build_video_handlers(
                app.state.blob_store,
                app.state.turn_attachments,
                app.state.llm_router,
                transcribe=(
                    WhisperCppClient(
                        settings.whisper_url,
                        settings.whisper_model,
                        timeout=settings.whisper_timeout,
                    )
                    if settings.whisper_url
                    else None
                ),
                transcribe_model=settings.whisper_model,
                gateway=LocalGatewayClient(settings.whisper_url) if settings.whisper_url else None,
            )
        app.state.agent_registry = build_registry(
            app.state.search_service,
            app.state.notes_repo,
            app.state.analysis_repo,
            app.state.agent_memory,
            app.state.agent_proposals,
            connector_registry,
            app.state.lists_repo,
            app.state.appointments_repo,
            app.state.wiki_read_store,
            build_wiki_write_handlers(app.state.notes_repo, app.state.job_queue, maker),
            app.state.location_repo,
            app.state.device_repo,
            web_handlers,
            app.state.city_geocoder,
            maker,
            external_reverse,
            router=app.state.llm_router,
            settings=settings_store,
            image_handlers=image_handlers,
            transcribe_handlers=transcribe_handlers,
            video_handlers=video_handlers,
            gmail_handlers=gmail_handlers,
        )
        app.state.agent_runlog = AgentRunLog(maker)
        app.state.run_reader = RunLogReader(maker)
        # The Automations operator surface: projects the live trigger/schedule/
        # pipeline config + the run log into the "when -> do" cards, and the action
        # registry into the Catalog. `seeded_names` is the subset mirrored into
        # app.actions (the shipped six, migration 0035); the rest are in-code only.
        app.state.automations_reader = AutomationsReader(
            maker,
            action_registry,
            frozenset(spec.name for spec in ACTION_SPECS),
        )
        app.state.agent_transcript = AgentTranscript(maker, app.state.turn_attachments)
        # Tasks: saved prompts that spawn an agent session on a schedule or on demand.
        # The runner reuses the same session/run/transcript stack /chat does, headless;
        # the scheduler loop (below) is the web-process driver (that's where the agent
        # stack lives and where "Run now" already executes).
        app.state.task_repo = TaskRepo(maker)
        app.state.task_runs = TaskRunRepo(maker)
        app.state.task_runner = TaskRunner(
            sessions=app.state.agent_sessions,
            runlog=app.state.agent_runlog,
            transcript=app.state.agent_transcript,
            runs=app.state.task_runs,
            executor=LoopTurnExecutor(app.state.llm_router, app.state.agent_registry),
            push=app.state.push_notifier,
        )
        tasks_loop_task = asyncio.create_task(
            run_tasks_loop(maker, app.state.task_repo, app.state.task_runner)
        )
        # Stopping a service is a synchronous `docker stop` on the supervisor — up to
        # the container's SIGTERM grace (ComfyUI's ~10 s) before it returns — so the
        # default 5 s httpx timeout would spuriously fail a stop that actually succeeds.
        app.state.supervisor_client = httpx.AsyncClient(
            base_url=settings.supervisor_url, timeout=30.0
        )
        yield
        if live_task is not None:
            live_task.cancel()
        tasks_loop_task.cancel()
        # Stop any chat turns still running detached from a (now-gone) SSE response, so
        # shutdown doesn't strand them; each closes via its own CancelledError path. AWAIT
        # their tasks (bounded) before disposing the engine: their cancel-cleanup runs the
        # run-log close inline, which opens a fresh pooled session — so the pool must
        # outlive it, or the close races a dead engine and strands the run in 'running'.
        live_turns = list(app.state.live_turns.values())
        for lt in live_turns:
            lt.cancel()
        tasks = [lt.task for lt in live_turns if getattr(lt, "task", None) is not None]
        if tasks:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10.0)
        # Same for any detached jcode coding turns (docs/proposed/JCODE_PLAN.md, Wave J2):
        # cancel + await before the engine is disposed, so their status-cleanup doesn't race
        # a dead pool and the upstream control-server stream is torn down.
        jcode_turns = list(getattr(app.state, "jcode_turns", {}).values())
        for jt in jcode_turns:
            jt.cancel()
        jcode_tasks = [jt.task for jt in jcode_turns if getattr(jt, "task", None) is not None]
        if jcode_tasks:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*jcode_tasks, return_exceptions=True), timeout=10.0
                )
        # And any background coder-warm tasks (jcode_warm_on_create): a warm sits inside
        # gateway.load() up to ~120s, so cancel + drain it rather than leave a pending
        # task to be destroyed at loop close.
        warm_tasks = list(getattr(app.state, "jcode_warm_tasks", set()))
        for wt in warm_tasks:
            wt.cancel()
        if warm_tasks:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*warm_tasks, return_exceptions=True), timeout=5.0
                )
        await app.state.supervisor_client.aclose()
        if image_gen_client is not None:
            await image_gen_client.aclose()
        await engine.dispose()

    app = FastAPI(title="JBrain", lifespan=lifespan)
    app.state.settings = settings
    # A live, process-local feed of debug-console activity so the web console can
    # show every /api/debug/* call as it lands — including ones run from outside
    # that browser tab. Only the verb/route/outcome are kept (no bodies).
    app.state.debug_activity = DebugActivity()
    # In-memory async-completion jobs (slow models behind a short proxy timeout):
    # job_id -> {status, result, error}, plus the live task refs so they aren't GC'd.
    app.state.debug_jobs = {}
    app.state.debug_job_tasks = set()

    if settings.debug_access_enabled:

        @app.middleware("http")
        async def _record_debug_activity(request: Request, call_next: Any) -> Any:
            response = await call_next(request)
            path = request.url.path
            # Skip the high-frequency poll endpoints so the feed doesn't record its
            # own reads (the activity feed and the job-status polling).
            if (
                path.startswith("/api/debug/")
                and not path.startswith("/api/debug/activity")
                and not path.startswith("/api/debug/jobs")
            ):
                # The handler stashes a short command summary (SQL/prompt/...) on
                # request.state; scope["state"] is shared, so it is readable here.
                app.state.debug_activity.record(
                    method=request.method,
                    path=path,
                    status=response.status_code,
                    client=request.headers.get("x-debug-client", ""),
                    detail=getattr(request.state, "debug_detail", ""),
                )
            return response

    app.include_router(health.router, prefix="/api")
    app.include_router(agent.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")
    app.include_router(appointments_api.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(chat_attachments.sessions_router, prefix="/api")
    app.include_router(chat_attachments.router, prefix="/api")
    app.include_router(chat_attachments.capabilities_router, prefix="/api")
    # Owner mint/list/revoke is always available (owner-gated; mint itself refuses
    # when the flag is off). The debug SURFACE mounts only when enabled, so a stock
    # deploy exposes no /api/debug/* routes at all.
    app.include_router(debug_tokens.router, prefix="/api")
    if settings.debug_access_enabled:
        app.include_router(debug.router, prefix="/api")
    app.include_router(devices.router, prefix="/api")
    app.include_router(family.router, prefix="/api")
    app.include_router(feed.router, prefix="/api")
    app.include_router(images.generated_router, prefix="/api")
    # The gallery list reads existing rows, so it is always available. The direct
    # generate/edit render endpoints mount only when image hosting is configured —
    # an unconfigured box 404s them (graceful degrade, mirroring the tool omission).
    app.include_router(images_render.list_router, prefix="/api")
    if settings.comfyui_url:
        app.include_router(images_render.router, prefix="/api")
    app.include_router(image_settings_api.router, prefix="/api")
    # Code mode (docs/proposed/JCODE_PLAN.md). Always mounted, but every route is
    # owner-gated and 404s when jcode isn't configured (app.state.jcode_client is None).
    app.include_router(jcode.router, prefix="/api")
    app.include_router(jcode_share.router, prefix="/api")
    app.include_router(jcode_terminal.router, prefix="/api")
    app.include_router(lists_api.router, prefix="/api")
    app.include_router(llm_settings_api.router, prefix="/api")
    app.include_router(locations.router, prefix="/api")
    app.include_router(live.router, prefix="/api")
    app.include_router(member.router, prefix="/api")
    # The MQTT broker's go-auth HTTP backend calls these on the internal network
    # only — NOT under /api (Caddy never routes /internal off-box).
    app.include_router(mqtt.router, prefix="/internal")
    app.include_router(notes.router, prefix="/api")
    app.include_router(ops.router, prefix="/api")
    app.include_router(owntracks.router, prefix="/api")
    app.include_router(pairing.router, prefix="/api")
    app.include_router(proposals.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(session_bridge.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(gmail_settings_api.router, prefix="/api")
    app.include_router(tasks_api.router, prefix="/api")
    app.include_router(tiles.router, prefix="/api")
    app.include_router(wiki.router, prefix="/api")
    return app


app = create_app()
