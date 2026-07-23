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
from jbrain.agent.brainevents import build_event_emitter, build_flag_emitter
from jbrain.agent.deepest_tool import DeepestHandle
from jbrain.agent.externaltools import build_external_handlers
from jbrain.agent.fetchtools import build_fetch_image_handlers
from jbrain.agent.gmailtools import build_gmail_handlers
from jbrain.agent.grabtools import build_grab_frame_handlers
from jbrain.agent.hurricanetools import build_hurricane_handlers
from jbrain.agent.imagegentools import build_image_handlers
from jbrain.agent.loop import ToolHandler
from jbrain.agent.media_results import MediaResults
from jbrain.agent.memory import MemoryRepo, MemoryService
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.readtools import build_registry
from jbrain.agent.researchtools import build_research_report_handlers
from jbrain.agent.runlog import AgentRunLog, RunLogReader
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.streamtools import build_stream_handlers
from jbrain.agent.transcribetools import build_transcribe_handlers
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.agent.videotools import build_video_handlers
from jbrain.agent.visiontools import build_compare_handlers
from jbrain.agent.weatherhistorytools import build_weather_history_handlers
from jbrain.agent.weathertools import build_weather_handlers
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
    brain,
    chat_attachments,
    debug,
    debug_tokens,
    devices,
    external_llm,
    family,
    feed,
    health,
    images,
    images_render,
    install,
    intake,
    jcode,
    jcode_llm,
    jcode_preview,
    jcode_share,
    jcode_terminal,
    live,
    locations,
    member,
    mqtt,
    notes,
    notifications,
    ops,
    owntracks,
    pairing,
    proposals,
    research_library,
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
from jbrain.api import pet as pet_api
from jbrain.api import settings as settings_api
from jbrain.api import (
    tasks as tasks_api,
)
from jbrain.api.debug_activity import DebugActivity
from jbrain.api.research_service import ResearchLibrary
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
from jbrain.intake.repo import SqlIntakeRepo
from jbrain.intake.sweep import intake_reaper_loop
from jbrain.jcode import JcodeClient
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.scheduler import run_jpet_loop
from jbrain.lists.repo import SqlListsRepo
from jbrain.llm import build_router
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.residency import ResidencyCoordinator
from jbrain.locations import SqlLocationRepo
from jbrain.locations.live import LiveBroadcaster, live_feeder
from jbrain.locations.pairing import SqlPairingRepo
from jbrain.locations.ratelimit import TokenBucket
from jbrain.locations.viewscope import SqlViewScopeRepo
from jbrain.media import ffmpeg_available
from jbrain.models.images import GeneratedImageRepo
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notify import NotifyBus
from jbrain.push import SqlFcmTokenRepo
from jbrain.queue import SYSTEM_CTX, PgJobQueue
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBackupShelf, FsBlobStore
from jbrain.stream import ytdlp_available
from jbrain.tasks.repo import TaskGroupRepo, TaskRepo, TaskRunRepo
from jbrain.tasks.runner import LoopTurnExecutor, TaskRunner
from jbrain.tasks.scheduler import run_tasks_loop
from jbrain.tiles import FsTileCache, HttpTileFetcher, TileService, TileSet, tile_cache_namespace
from jbrain.transcribe import WhisperCppClient
from jbrain.usage import SqlUsageRecorder
from jbrain.web import (
    FaviconFetcher,
    HurricaneClient,
    NhcGisClient,
    NhcSurgeClient,
    NwsClient,
    SearxngClient,
    WeatherClient,
    WeatherHistoryClient,
    WebFetcher,
)
from jbrain.wiki.actions import WIKI_SPECS
from jbrain.wiki.lint import WIKI_LINT_SPEC
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
    WIKI_LINT_SPEC,
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
        app.state.intake_repo = SqlIntakeRepo(maker)
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
        # Self-hosted owner notifications: the native app streams these over SSE and posts
        # them locally (task-ready, ...). Always on — it's in-process, no external service.
        app.state.notify_bus = NotifyBus()
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
        # Deferred media-analysis results (DEFERRED_TOOL_CALLS_PLAN.md P2): the store the
        # analyze_stream deferral writes to and the task_status card polls / cancels.
        app.state.media_results = MediaResults(maker)
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
        # The box's sole model evictor/restorer: ensure_room frees the fewest models to hold
        # the free-RAM floor before each local load (wired as the router's local_admit below),
        # free_room does the same for the settings screen's deliberate load, plan_load previews
        # an eviction without touching the box, and schedule_restore puts back whatever a
        # transient displacement (image render, code session) removed at end of turn instead of
        # cold-loading it. Inert on a cloud-only box (enabled off).
        app.state.residency = ResidencyCoordinator(
            app.state.local_gateway,
            windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
            models_dir=settings.local_models_dir,
            enabled=settings.local_llm_enabled,
            free_ram_fraction=settings.local_llm_free_ram_fraction,
        )
        # Serializes the jcode LLM proxy's model swaps (api.jcode_llm): one model loading/
        # serving at a time on the box, so a live grok `/model` switch (or a parallel agent)
        # cold-swaps instead of stacking two large models. Bound to this app's event loop.
        app.state.jcode_llm_swap_lock = asyncio.Lock()
        # Any API-side LLM call must flow through this router so its tokens
        # land in app.llm_usage like the worker's do. The overrides loader reads
        # the live per-task routing/reasoning settings (SYSTEM_CTX owner session)
        # on each call so the settings screen takes effect without a restart.
        app.state.llm_router = build_router(
            settings,
            recorder=SqlUsageRecorder(maker),
            overrides_loader=lambda: settings_store.llm_task_overrides(SYSTEM_CTX),
            local_windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
            # Evict-to-make-room before a local completion (co-residency budget).
            local_admit=app.state.residency.ensure_room,
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
        # (no owner data in context; docs/reference/ASSISTANT.md "Agent selection").
        # One best-effort emitter to the on-box wall display, shared by the web tools
        # (content-free markers) and the agent turn (opt-in LLM text streaming, gated on
        # the brain_llm_stream setting in jbrain.api.agent).
        brain_emit = build_event_emitter(settings.brain_events_url)
        app.state.brain_emit = brain_emit
        # A separate emitter for the wall's persistent config flags (read_aloud): boolean
        # display config, not owner text, so it is not gated by the per-turn text switch.
        app.state.brain_flag_emit = build_flag_emitter(settings.brain_events_url)
        # The wall base URL (events URL minus /event) — kept for any wall-direct call.
        app.state.brain_base_url = settings.brain_events_url.removesuffix("/event")
        # TTS moved into the `tts-stt` service: the authenticated /api/brain/tts +
        # /api/brain/voices proxy reaches its piper renderer here (so the PWA read-aloud +
        # voice picker never touch an unauthenticated service directly), and the tts_debug
        # flag is pushed to its /event (not the wall's).
        tts_base = settings.brain_tts_url.rstrip("/")
        app.state.brain_tts_base_url = tts_base
        app.state.brain_tts_flag_emit = build_flag_emitter(f"{tts_base}/event" if tts_base else "")
        web_fetcher = WebFetcher(reader_url=settings.reader_url)
        web_handlers = build_web_handlers(
            SearxngClient(settings.searxng_url),
            web_fetcher,
            emit=brain_emit,
        )
        # Fetches a source site's favicon on-box for web citation chips, so the PWA
        # renders a tappable logo without ever touching the third-party host (#9).
        app.state.favicon_fetcher = FaviconFetcher()
        # jerv's weather lookup (docs/reference/DESIGN.md "weather_card tool-view") — a direct,
        # pinned Open-Meteo upstream, the same sandboxed-web posture as search. Merged
        # into the web handlers so it rides the existing `web` permission gate; the
        # offline city geocoder (set below) keeps the owner's precise fix on-box.
        weather_client = WeatherClient(
            settings.open_meteo_forecast_url, settings.open_meteo_geocode_url
        )
        # jerv's historical weather lookup — the Open-Meteo Archive twin of the forecast
        # tool. It reuses weather_client's geocoder so the location firewall is identical;
        # it fetches the hourly past record and computes the heat index on-box (the
        # per-year figure web search can't find). Merged into the web handlers below.
        weather_history_client = WeatherHistoryClient(settings.open_meteo_archive_url)
        # jerv's hurricane lookup (DESIGN.md "hurricane_card tool-view") — a direct,
        # pinned NHC upstream (the global active-storm list, no query), the same
        # sandboxed-web posture as weather. It reuses the weather geocoder + the
        # offline city geocoder so the location firewall is identical; distance and
        # bearing to a storm are computed on-box.
        hurricane_client = HurricaneClient(settings.nhc_current_storms_url)
        # The tabbed hurricane card's detail feeds (docs/archive/HURRICANE_TABS_PLAN.md): the
        # forecast track + cone (NHC GIS, queried by storm identity — no location), and
        # the official alert + local timeline (NWS) + peak-surge band (NHC), queried by
        # the geocoded city centre only. All free, no key; each degrades gracefully.
        nhc_gis_client = NhcGisClient(settings.nhc_tropical_mapserver_url)
        nws_client = NwsClient(settings.nws_api_url)
        nhc_surge_client = NhcSurgeClient(settings.nhc_surge_mapserver_url)
        # The archivist persona's Gmail tools. Always wired over a provider that reads
        # the OAuth credentials live from the settings panel (env fallback), so a saved
        # change takes effect with no restart; until a refresh token exists the tools
        # report "connect Gmail in Settings" (docs/archive/EMAIL_ARCHIVIST_PLAN.md).
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
        web_handlers.update(build_weather_handlers(weather_client, app.state.city_geocoder))
        web_handlers.update(
            build_weather_history_handlers(
                weather_history_client, weather_client, app.state.city_geocoder
            )
        )
        web_handlers.update(
            build_hurricane_handlers(
                hurricane_client,
                weather_client,
                app.state.city_geocoder,
                nhc_gis_client,
                nws_client,
                nhc_surge_client,
            )
        )
        external_reverse = NominatimReverseClient(settings.external_geocoder_url)
        # Built before the registry: edit_image resolves a chat attachment's bytes
        # through the same TurnAttachmentRepo, so it must exist first.
        app.state.agent_sessions = AgentSessionRepo(maker)
        app.state.turn_attachments = TurnAttachmentRepo(maker, app.state.agent_sessions)
        # jerv's local image generator (docs/archive/IMAGE_GEN_PLAN.md). Wired only when a
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
                # Freeing the LLMs for a render is a displacement: record what it evicts so the
                # end-of-turn restore puts the box back to its pre-render steady state.
                on_evicted=app.state.residency.note_evicted,
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
        # jerv's on-box audio transcription (docs/archive/WHISPER_TRANSCRIPTION_PLAN.md).
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
        # Code mode (docs/archive/JCODE_PLAN.md, Wave J2): the api proxies an owner's
        # sandboxed coding session to the internal jcode control server. Wired only when
        # configured — the owner-gated routes 404 otherwise (graceful degrade). The
        # session is driven through its interactive terminal (a WS PTY); there is no
        # turn/SSE surface.
        app.state.jcode_client = (
            JcodeClient(settings.jcode_url, settings.jcode_token) if settings.jcode_url else None
        )
        # jerv's on-box video analysis (docs/archive/VIDEO_ANALYSIS_PLAN.md): sample + caption
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
        # jerv's URL-sourced stream/video analysis (docs/archive/STREAM_ANALYSIS_PLAN.md):
        # resolve a video URL with yt-dlp and sample it with ffmpeg, then reuse the
        # analyze_video caption→fuse→reduce core. Wired only when BOTH ffmpeg and yt-dlp
        # are present; the registry drops the `analyze_stream` sidecar otherwise (graceful
        # degrade). Whisper is optional — frames-only without it, like analyze_video.
        stream_handlers: dict[str, ToolHandler] = {}
        if ffmpeg_available() and ytdlp_available():
            stream_handlers = build_stream_handlers(
                app.state.blob_store,
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
                # The deferred path (DEFERRED_TOOL_CALLS_PLAN.md P2): a full/long analysis
                # kicks the analyze_stream_url job and stores its result for the status card.
                queue=app.state.job_queue,
                media_results=app.state.media_results,
            )
        # jerv's single-frame grab (VIDEO_IMAGE_TOOLS_PLAN.md): extract a still from a
        # video URL or attachment at a timestamp and persist it as a first-class chat
        # image (analyze_image/compare_images read it by id). Wired only when ffmpeg can
        # sample frames; the URL path also uses yt-dlp (degrades cleanly without it).
        grab_handlers: dict[str, ToolHandler] = {}
        if ffmpeg_available():
            grab_handlers = build_grab_frame_handlers(
                app.state.blob_store,
                app.state.turn_attachments,
                app.state.generated_image_repo,
                maker,
                app.state.llm_router,
            )
        # jerv's web-image fetch (VIDEO_IMAGE_TOOLS_PLAN.md): fetch a web image's bytes
        # through the same SSRF-guarded fetcher web_fetch uses and persist it as a chat
        # image (analyze_image/compare_images read it by id) — jerv's only way to see a
        # picture on the web, since web_fetch is text-only. Always wired (the fetcher +
        # image storage always exist); jerv reaches it by allowlist, curator never does.
        fetch_image_handlers = build_fetch_image_handlers(
            web_fetcher,
            app.state.blob_store,
            app.state.generated_image_repo,
            maker,
            emit=brain_emit,
        )
        # jerv's multi-image compare (VIDEO_IMAGE_TOOLS_PLAN.md): compare N chat images with
        # the vision model and show the owner a side-by-side. Router-gated (a vision read
        # needs no ComfyUI); always wired here since the router always exists.
        compare_handlers = build_compare_handlers(
            app.state.llm_router,
            app.state.blob_store,
            app.state.generated_image_repo,
            app.state.turn_attachments,
            maker,
        )
        deepest_handle = DeepestHandle()
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
            stream_handlers=stream_handlers,
            grab_handlers=grab_handlers,
            fetch_image_handlers=fetch_image_handlers,
            compare_handlers=compare_handlers,
            gmail_handlers=gmail_handlers,
            external_handlers=build_external_handlers(
                maker,
                TeiEmbedClient(settings.embed_url),
                blobs=app.state.blob_store,
                proposals=app.state.agent_proposals,
            ),
            research_report_handlers=build_research_report_handlers(
                maker,
                TeiEmbedClient(settings.embed_url),
                proposals=app.state.agent_proposals,
            ),
            # Deepest-research off-turn transports (R6): the NotifyBus deep-link nudge and the
            # FCM poke (owner tokens resolved live per tick); `deepest_handle` hands the wired
            # kickoff service back out for the lifespan resume/drain hooks below.
            notify_bus=app.state.notify_bus,
            push=app.state.push_notifier,
            fcm_token_repo=app.state.fcm_token_repo,
            deepest_handle=deepest_handle,
        )
        # The background deepest-research supervisor (resume interrupted runs at startup,
        # drain in-flight ones at shutdown); None when deepest isn't wired (no router).
        app.state.deepest = deepest_handle.service
        # A restart (deploy/crash) kills the lane's in-process tasks, so re-drive any run left
        # 'running' in the checkpoint table. Detached so a slow DB never blocks boot; stored so
        # the task isn't GC'd. Best-effort inside resume_interrupted (never raises into boot).
        app.state.deepest_resume_task = (
            asyncio.create_task(app.state.deepest.resume_interrupted())
            if app.state.deepest is not None
            else None
        )
        app.state.agent_runlog = AgentRunLog(maker)
        app.state.run_reader = RunLogReader(maker)
        # The owner-facing Research Library reader: browse/search/delete over jerv's
        # persisted deep-research reports + analysed videos (the external corpus).
        app.state.research_library = ResearchLibrary(
            maker, app.state.embed_client, app.state.blob_store
        )
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
        app.state.task_groups = TaskGroupRepo(maker)
        app.state.task_runs = TaskRunRepo(maker)
        app.state.task_runner = TaskRunner(
            sessions=app.state.agent_sessions,
            runlog=app.state.agent_runlog,
            transcript=app.state.agent_transcript,
            runs=app.state.task_runs,
            executor=LoopTurnExecutor(app.state.llm_router, app.state.agent_registry),
            push=app.state.push_notifier,
            notify=app.state.notify_bus,
        )
        tasks_loop_task = asyncio.create_task(
            run_tasks_loop(maker, app.state.task_repo, app.state.task_runner)
        )
        # The guided-intake reaper: abandons stale drafting intake sessions (§6), under the
        # full-owner system context so it can sweep every link's sessions.
        intake_reaper_task = asyncio.create_task(
            intake_reaper_loop(app.state.intake_repo, SYSTEM_CTX)
        )
        # JPet drives tick: advances the family wall-pet's needs on a clock, in the web
        # process (pure arithmetic, never the job queue → the pet takes second seat).
        # The broadcaster fans each tick/command state change out to the Wall + phone
        # Control screen over /api/pet/stream so both surfaces stay in sync.
        app.state.jpet_repo = SqlJpetRepo(maker)
        app.state.pet_broadcaster = PetBroadcaster()
        # Ephemeral JPet wall effects ("turn X <colour>" / "make X bigger" / "be a dragon") —
        # in-memory only, never persisted, cleared when the wall reloads or on "reset everything"
        # (POST /internal/pet/effects/clear).
        app.state.pet_effects = {"colors": {}, "scales": {}, "pet_scale": 1.0, "pet_form": "robot"}
        # Bounds the on-box wall's voice listener (unauthenticated, LAN-only) so it can't flood
        # the local LLM: a burst of ~8 spoken commands, refilling ~1 every 2.5s.
        app.state.pet_say_rate_limiter = TokenBucket(capacity=8, refill_per_sec=0.4)
        jpet_loop_task = asyncio.create_task(
            run_jpet_loop(
                maker,
                app.state.jpet_repo,
                domain=settings.jpet_domain,
                name=settings.jpet_name,
                interval=settings.jpet_tick_seconds,
                broadcaster=app.state.pet_broadcaster,
            )
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
        intake_reaper_task.cancel()
        jpet_loop_task.cancel()
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
        # Any background coder-warm tasks (the explicit /jcode/model/warm): a warm sits inside
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
        # Drain in-flight deepest runs: cancel + AWAIT (bounded) so each records a terminal
        # status via run_deepest's cancel path — which opens a fresh session — BEFORE the
        # engine is disposed, the same pool-outlives-cleanup ordering the live turns need.
        resume_task = getattr(app.state, "deepest_resume_task", None)
        if resume_task is not None:
            resume_task.cancel()
        deepest = getattr(app.state, "deepest", None)
        if deepest is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(deepest.drain(), timeout=10.0)
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
    app.include_router(brain.router, prefix="/api")
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
    # Public, unauthenticated setup-script delivery (irm .../install/grok.ps1 | iex).
    # Carries no secrets; the script prompts for the access token at runtime.
    app.include_router(install.router, prefix="/api")
    # Guided-intake share links (docs/archive/GUIDED_INTAKE_PLAN.md). Owner management is
    # owner-gated; /intake/redeem is public (the secret is the credential).
    app.include_router(intake.router, prefix="/api")
    # Code mode (docs/archive/JCODE_PLAN.md). Always mounted, but every route is
    # owner-gated and 404s when jcode isn't configured (app.state.jcode_client is None).
    app.include_router(jcode.router, prefix="/api")
    app.include_router(jcode_llm.router, prefix="/api")
    app.include_router(jcode_share.router, prefix="/api")
    app.include_router(jcode_terminal.router, prefix="/api")
    # The host-mode web preview proxy (docs/archive/JCODE_PREVIEW_HOST_PLAN.md). NOT under /api:
    # Caddy host-routes <slug>-preview.<host> to /__jcode_preview/{slug} on the preview
    # subdomain only (the main site 404s it), and the unguessable slug is the auth.
    app.include_router(jcode_preview.router)
    app.include_router(external_llm.router, prefix="/api")
    app.include_router(lists_api.router, prefix="/api")
    app.include_router(pet_api.router, prefix="/api")
    app.include_router(llm_settings_api.router, prefix="/api")
    app.include_router(locations.router, prefix="/api")
    app.include_router(live.router, prefix="/api")
    app.include_router(member.router, prefix="/api")
    # The MQTT broker's go-auth HTTP backend calls these on the internal network
    # only — NOT under /api (Caddy never routes /internal off-box).
    app.include_router(mqtt.router, prefix="/internal")
    # The on-box wall display reads the pet snapshot here (internal
    # network only; read-only; safe 'general' domain) — never off-box via Caddy.
    app.include_router(pet_api.internal_router, prefix="/internal")
    app.include_router(notes.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(ops.router, prefix="/api")
    app.include_router(owntracks.router, prefix="/api")
    app.include_router(pairing.router, prefix="/api")
    app.include_router(proposals.router, prefix="/api")
    app.include_router(research_library.router, prefix="/api")
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
