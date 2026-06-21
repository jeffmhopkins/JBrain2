import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.correctionmine import CORRECTION_MINE_SPEC
from jbrain.agent.imagegentools import build_image_handlers
from jbrain.agent.loop import ToolHandler
from jbrain.agent.memory import MemoryRepo, MemoryService
from jbrain.agent.predicatereview import PREDICATE_REVIEW_SPEC
from jbrain.agent.promptselfedit import PROMPT_SELF_EDIT_SPEC
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.readtools import build_registry
from jbrain.agent.runlog import AgentRunLog, RunLogReader
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.skilldistill import SKILL_DISTILL_SPEC
from jbrain.agent.skills import SkillService, SkillsRepo
from jbrain.agent.skillsweep import SKILL_SWEEP_SPEC
from jbrain.agent.transcript_store import AgentTranscript
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
    devices,
    family,
    feed,
    health,
    images,
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
from jbrain.api import image_settings as image_settings_api
from jbrain.api import lists as lists_api
from jbrain.api import llm_settings as llm_settings_api
from jbrain.api import settings as settings_api
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
from jbrain.image_gen.comfyui import ComfyUiImageGen
from jbrain.image_gen.gateway import ComfyUiGatewayClient
from jbrain.lists.repo import SqlListsRepo
from jbrain.llm import build_router
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.locations import SqlLocationRepo
from jbrain.locations.live import LiveBroadcaster, live_feeder
from jbrain.locations.pairing import SqlPairingRepo
from jbrain.locations.ratelimit import TokenBucket
from jbrain.locations.viewscope import SqlViewScopeRepo
from jbrain.models.images import GeneratedImageRepo
from jbrain.notes.repo import SqlNotesRepo
from jbrain.push import SqlFcmTokenRepo
from jbrain.queue import SYSTEM_CTX, PgJobQueue
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBackupShelf, FsBlobStore
from jbrain.tiles import FsTileCache, HttpTileFetcher, TileService, TileSet, tile_cache_namespace
from jbrain.usage import SqlUsageRecorder
from jbrain.web import SearxngClient, WebFetcher
from jbrain.wiki.actions import WIKI_SPECS
from jbrain.wiki.readstore import WikiReadStore
from jbrain.wiki.talkstore import WikiTalkStore
from jbrain.workflow.automations import AutomationsReader
from jbrain.workflow.evalaction import EVAL_RUN_SPEC
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.engine = engine
        app.state.session_maker = maker
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
        # Per-device ingest cap: 60 fixes/min sustained (burst 60). A flooding
        # device gets a 429 and backs off; normal move-mode never trips it.
        app.state.location_rate_limiter = TokenBucket(capacity=60, refill_per_sec=1.0)
        app.state.notes_repo = SqlNotesRepo(maker)
        app.state.lists_repo = SqlListsRepo(maker)
        app.state.appointments_repo = SqlAppointmentsRepo(maker)
        app.state.blob_store = FsBlobStore(settings.blob_dir)
        app.state.generated_image_repo = GeneratedImageRepo()
        app.state.backup_shelf = FsBackupShelf(settings.backups_dir)
        app.state.job_queue = PgJobQueue(maker)
        # The action registry the emergency-trigger control resolves a sweep's
        # pipeline through (workflow/scheduler.fire_trigger) and the Automations
        # surface renders the Catalog from. Mirrors the worker's composed registry
        # EXACTLY — the shipped six plus every in-code action (purge, the three
        # reconcilers, the geofence sweep, the opt-in eval_run, the Loop 2-4
        # self-improvement actions, the Phase-6 hygiene sweeps, and the wiki builder)
        # — so any manual trigger fired from Ops resolves to the same handler the
        # scheduler would (else registry.get raises ActionRegistryError), and the
        # Catalog lists the full set the worker can run. Keep in lockstep with the
        # worker's build_registry composition.
        action_registry = build_action_registry(
            (
                *ACTION_SPECS,
                PURGE_ACTION,
                RECONCILE_PENDING_NOTES_ACTION,
                RECONCILE_PENDING_INTEGRATION_ACTION,
                RECONCILE_UNEMBEDDED_NOTES_ACTION,
                GEOFENCE_SWEEP_ACTION,
                EVAL_RUN_SPEC,
                # The Loop 2-4 self-improvement actions — seeded manual=true, so they
                # must resolve from Ops too (they were worker-only before).
                SKILL_DISTILL_SPEC,
                SKILL_SWEEP_SPEC,
                PREDICATE_REVIEW_SPEC,
                CORRECTION_MINE_SPEC,
                PROMPT_SELF_EDIT_SPEC,
                # The Phase-6 hygiene sweeps, so their seeded manual triggers resolve from
                # Ops (POST /ops/triggers/{id}/run -> registry.get) — emergency-fireable.
                ENTITY_HYGIENE_SPEC,
                REEMBED_SPEC,
                TAG_CONSOLIDATE_SPEC,
                *WIKI_SPECS,
            )
        )
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
        app.state.skills_repo = SkillsRepo(maker)
        app.state.skill_service = SkillService(
            app.state.skills_repo, TeiEmbedClient(settings.embed_url), settings.embed_model
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
            app.state.image_gen = ComfyUiImageGen(settings.comfyui_url, image_gen_client)
            # The management client (status/free) for the owner image-settings surface
            # — the sibling of app.state.local_gateway, wired on the same gate.
            app.state.comfyui_gateway = ComfyUiGatewayClient(settings.comfyui_url)
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
            )
        else:
            app.state.image_gen = None
            app.state.comfyui_gateway = None
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
            external_reverse,
            router=app.state.llm_router,
            settings=settings_store,
            image_handlers=image_handlers,
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
        # Stopping a service is a synchronous `docker stop` on the supervisor — up to
        # the container's SIGTERM grace (ComfyUI's ~10 s) before it returns — so the
        # default 5 s httpx timeout would spuriously fail a stop that actually succeeds.
        app.state.supervisor_client = httpx.AsyncClient(
            base_url=settings.supervisor_url, timeout=30.0
        )
        yield
        if live_task is not None:
            live_task.cancel()
        await app.state.supervisor_client.aclose()
        if image_gen_client is not None:
            await image_gen_client.aclose()
        await engine.dispose()

    app = FastAPI(title="JBrain", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    app.include_router(agent.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")
    app.include_router(appointments_api.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(chat_attachments.sessions_router, prefix="/api")
    app.include_router(chat_attachments.router, prefix="/api")
    app.include_router(chat_attachments.capabilities_router, prefix="/api")
    app.include_router(devices.router, prefix="/api")
    app.include_router(family.router, prefix="/api")
    app.include_router(feed.router, prefix="/api")
    app.include_router(images.generated_router, prefix="/api")
    app.include_router(image_settings_api.router, prefix="/api")
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
    app.include_router(tiles.router, prefix="/api")
    app.include_router(wiki.router, prefix="/api")
    return app


app = create_app()
