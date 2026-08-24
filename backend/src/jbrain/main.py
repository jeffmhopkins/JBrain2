import asyncio
import datetime as dt
import functools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain import box_events
from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.brainevents import (
    build_event_emitter,
    build_flag_emitter,
    build_value_emitter,
)
from jbrain.agent.continuation import PlanContinuationRunner, run_plan_continuation_loop
from jbrain.agent.croptools import build_crop_handlers
from jbrain.agent.deepest_tool import DeepestHandle
from jbrain.agent.drawtools import build_canvas_handlers
from jbrain.agent.externaltools import build_external_handlers
from jbrain.agent.fetchtools import build_fetch_image_handlers
from jbrain.agent.gmailtools import build_gmail_handlers
from jbrain.agent.grabtools import build_grab_frame_handlers
from jbrain.agent.grokipediatools import build_grokipedia_handlers
from jbrain.agent.htmltools import build_html_handlers
from jbrain.agent.hurricanetools import build_hurricane_handlers
from jbrain.agent.imagegentools import build_image_handlers
from jbrain.agent.loop import ToolHandler
from jbrain.agent.media_results import MediaResults
from jbrain.agent.memory import MemoryRepo, MemoryService
from jbrain.agent.ocrtools import build_ocr_handlers
from jbrain.agent.portaltools import build_portal_handlers
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.publicrecordstools import build_public_records_handlers
from jbrain.agent.readtools import build_registry
from jbrain.agent.researchtools import build_research_report_handlers
from jbrain.agent.runlog import AgentRunLog, RunLogReader, reap_stranded_loop
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.streamtools import build_stream_handlers
from jbrain.agent.tool_artifacts import ToolArtifactRepo
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
    jlaunch,
    jlaunch_share,
    jlaunch_terminal,
    live,
    locations,
    member,
    mqtt,
    notes,
    notifications,
    ops,
    owntracks,
    pairing,
    plans,
    proposals,
    research_library,
    research_share,
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
from jbrain.api import tavily_settings as tavily_settings_api
from jbrain.api.debug_activity import DebugActivity
from jbrain.api.research_service import ResearchLibrary
from jbrain.appointments.repo import SqlAppointmentsRepo
from jbrain.auth.repo import SqlAuthRepo
from jbrain.captions import fetch_caption_transcript
from jbrain.citygeocode import CityGeocoder
from jbrain.config import Settings, get_settings
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.geocoding import geocode_connectors
from jbrain.connectors.medical import medical_connectors
from jbrain.connectors.repo import SqlConnectorCache
from jbrain.connectors.service import ConnectorService
from jbrain.db.session import scoped_session
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.embed import TeiEmbedClient
from jbrain.family import SqlFamilyRepo
from jbrain.geocode import NominatimReverseClient
from jbrain.gmail import GmailClientProvider
from jbrain.gmail.triage import TRIAGE_INBOX_SPEC
from jbrain.htmlrender import HtmlRenderClient
from jbrain.image_gen.comfyui import ComfyUiImageGen
from jbrain.image_gen.gateway import ComfyUiGatewayClient
from jbrain.image_gen.render import ImageRenderService
from jbrain.intake.repo import SqlIntakeRepo
from jbrain.intake.sweep import intake_reaper_loop
from jbrain.jcode import JcodeClient
from jbrain.jlaunch import JlaunchClient
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.scheduler import run_jpet_loop
from jbrain.lists.repo import SqlListsRepo
from jbrain.llm import build_router, gpu_guard
from jbrain.llm.kv_prefix import KvPrefixStore
from jbrain.llm.ledger import ReservationLedger
from jbrain.llm.local_gateway import LocalGatewayClient
from jbrain.llm.residency import (
    ResidencyCoordinator,
    ResidencyWiring,
    pg_box_lock,
    pg_box_try_lock,
)
from jbrain.llm.warm_keeper import WarmKeeper
from jbrain.locations import SqlLocationRepo
from jbrain.locations.live import LiveBroadcaster, live_feeder
from jbrain.locations.pairing import SqlPairingRepo
from jbrain.locations.ratelimit import TokenBucket
from jbrain.locations.viewscope import SqlViewScopeRepo
from jbrain.media import ffmpeg_available
from jbrain.models.images import GeneratedImageRepo
from jbrain.models.telemetry import DeployHistoryRepo
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notify import NotifyBus
from jbrain.push import SqlFcmTokenRepo
from jbrain.queue import SYSTEM_CTX, PgJobQueue
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBackupShelf, FsBlobStore
from jbrain.stream import resolve_stream, ytdlp_available
from jbrain.tasks.repo import TaskGroupRepo, TaskRepo, TaskRunRepo
from jbrain.tasks.runner import LoopTurnExecutor, TaskRunner
from jbrain.tasks.scheduler import _owner_principal_id, run_tasks_loop
from jbrain.tiles import FsTileCache, HttpTileFetcher, TileService, TileSet, tile_cache_namespace
from jbrain.transcribe import WhisperCppClient
from jbrain.usage import SqlUsageRecorder
from jbrain.vision import RapidOcrClient
from jbrain.vitals_ring import VitalsRing, sample_loop
from jbrain.web import (
    CourtListenerClient,
    DomainSkipRepo,
    FaviconFetcher,
    FederalRegisterClient,
    FeedClient,
    GrokipediaClient,
    HurricaneClient,
    NhcGisClient,
    NhcSurgeClient,
    NppesClient,
    NwsClient,
    SearxngClient,
    WeatherClient,
    WeatherHistoryClient,
    WebFetcher,
    WikidataClient,
)
from jbrain.web.portals import FlDfsResolver, FlSunbizResolver
from jbrain.web.youtube import youtube_page
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


def _prefix_lost_notifier(app: FastAPI) -> Callable[[str], None]:
    """Bridge residency → WarmKeeper without an import cycle or a construction-order
    constraint: residency reports a served name whose primed KV it just dropped, and the
    keeper forgets its memo so the next tick re-primes on the eager cadence."""

    def notify(served_model: str) -> None:
        keeper = getattr(app.state, "warm_keeper", None)
        if keeper is not None:
            keeper.note_prefix_lost(served_model)

    return notify


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.engine = engine
        app.state.session_maker = maker
        # The box's narration of its own GPU work (model loads, evictions, renders), which
        # the vitals surface reads back so a pinned trace with an empty roster explains
        # itself. Wired per PROCESS, not per request: the writers sit inside the gateway
        # client, which has no session of its own. See jbrain.box_events.
        box_events.configure(maker, source="api")
        # When this process came up — the debug /version route pairs it with the
        # image's baked build_time so an operator can tell a freshly-published build
        # from one still running behind an un-recreated container.
        app.state.started_at = dt.datetime.now(dt.UTC)
        # Record the running build in deploy_history so any timestamped record can later
        # be tied to the version that was live when it was produced. Only for a STAMPED
        # image (a real deploy carries a git_sha) — a dev/un-stamped build is skipped, so
        # this never touches the DB in tests — and strictly best-effort: a telemetry write
        # must never block or fail app startup. record_if_changed dedupes a plain restart.
        if settings.git_sha != "unknown":
            try:
                async with scoped_session(maker, SYSTEM_CTX) as session:
                    await DeployHistoryRepo().record_if_changed(
                        session,
                        git_sha=settings.git_sha,
                        git_describe=settings.git_describe,
                        build_time=settings.build_time,
                    )
            except Exception:  # noqa: BLE001 - telemetry must never crash boot
                structlog.get_logger().warning("deploy_history.record_failed", exc_info=True)
        # In-flight chat turns, detached from their SSE response so a backgrounded PWA
        # can't kill them; keyed by run_id for the Stop endpoint and shutdown cleanup.
        app.state.live_turns = {}
        # "A turn is starting for this session" markers (session_id → monotonic ts), set by
        # /chat before it registers in live_turns, read by the plan-continuation sweep so it
        # yields to an owner turn mid-startup (JERV_PLANNING_TOOL_PLAN.md).
        app.state.turn_starting = {}
        # "A foreground client is watching this session's plan" markers (session_id → monotonic
        # ts), set by the live-run poll endpoint, read by the plan-continuation sweep so a step
        # fired while the owner is present runs SUPERVISED — the lifted per-turn budget.
        app.state.plan_presence = {}
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
        # Clear any stranded code-mode box reservation at startup. The flag is persisted, but a
        # crash/reboot mid-code-session (e.g. earlyoom killing a process — the very incident this
        # guards) would otherwise leave it set with no code session running, wedging the box:
        # residency would refuse every load and the worker would stay paused, while the launcher
        # shows code mode OFF (it reads live service state, not the flag). Boot is a safe reset —
        # no model is warm yet — so releasing here can only cost a re-toggle if a session really
        # was live across the restart, never a permanent wedge. Best-effort; a DB hiccup here
        # must not block startup (the reservation simply isn't cleared, no worse than today).
        with suppress(Exception):
            await settings_store.set_code_mode_hold_names(SYSTEM_CTX, [])
        # Budget and WATCH every load against the iGPU's device pool (GTT), not just system
        # RAM. The two are accounted separately on an APU, and counting only system RAM let
        # loads whose real device cost far exceeded their catalog estimate freeze this host —
        # three times, once with ~105 GiB of system RAM free. The supervisor already reads
        # these counters for the Ops screen; this points the load decision at them.
        # LATE-BOUND, like on_prefix_lost below: the supervisor client is created further down
        # this same startup, so the lambda resolves it at call time and degrades to an
        # unmeasurable pool (unguarded loads, today's behaviour) if it never appears.
        gpu_probe = gpu_guard.SupervisorGpuMemProbe(
            lambda: getattr(app.state, "supervisor_client", None), settings.supervisor_token
        )
        # Admin client for the local-model gateway (runtime loaded-state + unload).
        # Best-effort; the settings screen tolerates it being unreachable.
        # The probe goes on the GATEWAY, not just the residency coordinator: `load()` is the
        # one chokepoint every caller passes through, so a guard there is one no caller can
        # skip — which is precisely how the three freezes got past the coordinator's guard.
        # The window/slot loaders make the guard reserve for the window llama-swap will REALLY
        # serve (the operator's `-c` override), not the catalog default — KV is linear in it.
        # The reservation ledger (docs/plans/LOCAL_MODEL_LEDGER_PLAN.md), AUTHORITATIVE: it
        # charges every real load this process runs and refuses ones that don't fit. One
        # instance, handed to BOTH the gateway (which charges through it) and the residency
        # coordinator below (which plans evictions with its arithmetic) — two readers of one
        # set of rows, never two budgets.
        api_reservations = ReservationLedger(
            maker, source="api", device_probe=gpu_probe, shadow=False
        )
        app.state.local_gateway = LocalGatewayClient(
            settings.local_llm_url,
            gpu_probe=gpu_probe,
            reservations=api_reservations,
            windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
            slots_loader=lambda: settings_store.llm_local_parallel_slots(SYSTEM_CTX),
            # Re-stamp the gateway config HERE rather than on every settings edit: rewriting
            # it makes llama-swap reload, and its reload kills every running model, not just
            # the edited one. See LocalGatewayClient._config_regen.
            config_regen=lambda: llm_settings_api.regen_gateway_config(settings, settings_store),
            # Lets a finished load drop the page-cache copy of the weights it just read.
            models_dir=settings.local_models_dir,
        )
        # The box's sole model evictor/restorer: ensure_room frees the fewest models to hold
        # the free-RAM floor before each local load (passed to build_router below as its
        # residency, so every local completion admits through this same instance),
        # free_room does the same for the settings screen's deliberate load, plan_load previews
        # an eviction without touching the box, and schedule_restore puts back whatever a
        # transient displacement (image render, code session) removed at end of turn instead of
        # cold-loading it. Inert on a cloud-only box (enabled off).
        app.state.residency = ResidencyCoordinator(
            app.state.local_gateway,
            ResidencyWiring(
                windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
                slots_loader=lambda: settings_store.llm_local_parallel_slots(SYSTEM_CTX),
                models_dir=settings.local_models_dir,
                enabled=settings.local_llm_enabled,
                free_ram_fraction=settings.local_llm_free_ram_fraction,
                # The live operator override of the floor (Settings → LLM); falls back to the
                # free_ram_fraction config default above when unset. Read per load, so a change
                # applies with no restart. Wired identically in the worker (jbrain.worker).
                fraction_loader=lambda: settings_store.llm_local_free_ram_fraction(SYSTEM_CTX),
                # Code-mode box reservation (jcode power ON writes it): while set, ensure_room
                # refuses to load any model outside code mode's reserved set, so nothing evicts its
                # models or co-loads past physical RAM. Read per load (SYSTEM_CTX), identically
                # wired in the worker.
                hold_loader=lambda: settings_store.code_mode_hold_names(SYSTEM_CTX),
                # The operator's end-of-turn restore switch (Settings → LLM). Read per restore,
                # so flipping it takes effect on the next turn with no restart.
                auto_restore_loader=lambda: settings_store.llm_local_auto_restore(SYSTEM_CTX),
                # Serialize evict+load against the worker process (which runs its own coordinator
                # over the same box) so a deferred worker load can't co-load past the floor here.
                box_lock=pg_box_lock(maker),
                # Restore's lock is the NON-blocking one. It must skip rather than queue: a restore
                # that waits is restoring to a steady state another process is already changing,
                # and while it waits it can hold the per-process load lock against a chat turn.
                box_try_lock=pg_box_try_lock(maker),
                # Tell the WarmKeeper when an eviction or a bare restore-load drops a model's
                # primed prefix. LATE-BOUND on purpose: the keeper is constructed further down
                # this same startup, so the lambda resolves it at call time and degrades to a
                # no-op on a build with no agent wired.
                on_prefix_lost=_prefix_lost_notifier(app),
                # Same probe the gateway guards with, here for the post-load MEASUREMENT: what a
                # load actually cost in device memory, logged beside what the catalog predicted,
                # so those numbers get corrected from data rather than from the next freeze.
                gpu_probe=gpu_probe,
                # The SAME ledger instance the gateway charges through, so the eviction plan
                # and the admission verdict come from one arithmetic (L3).
                ledger=api_reservations,
            ),
        )
        # Serializes the jcode LLM proxy's model swaps (api.jcode_llm): one model loading/
        # serving at a time on the box, so a live grok `/model` switch (or a parallel agent)
        # cold-swaps instead of stacking two large models. Bound to this app's event loop.
        app.state.jcode_llm_swap_lock = asyncio.Lock()
        # Any API-side LLM call must flow through this router so its tokens
        # land in app.llm_usage like the worker's do. The overrides loader reads
        # the live per-task routing/reasoning settings (SYSTEM_CTX owner session)
        # on each call so the settings screen takes effect without a restart.
        # Read the Fast-Qwen-loads patch setting ONCE here: it gates whether the qwen3.8
        # MTP-hybrids are admitted to the kv-prefix disk layer, and its true source of truth is
        # the running llama-server build, which only changes on the gateway rebuild that also
        # recreates this container — so a startup read is re-taken exactly when it can change.
        # Best-effort: a DB hiccup degrades to OFF (the conservative, stock-engine behaviour).
        kv_patch_active = False
        with suppress(Exception):
            kv_patch_active = await settings_store.local_llm_patch_restore_checkpoint(SYSTEM_CTX)
        app.state.kv_prefix = KvPrefixStore(
            app.state.local_gateway, settings.local_models_dir, patch_active=kv_patch_active
        )
        app.state.llm_router = build_router(
            settings,
            recorder=SqlUsageRecorder(maker),
            overrides_loader=lambda: settings_store.llm_task_overrides(SYSTEM_CTX),
            local_windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
            # The router admits every local load through THIS coordinator — the same
            # instance the settings screen (plan_load/free_room) and the chat endpoint
            # (schedule_restore/note_evicted) drive, so admission and displacement
            # bookkeeping stay coherent.
            residency=app.state.residency,
            # Turns on the prefill diagnostic: on a local turn slow to say anything, this
            # reads `/slots` off the SAME gateway the coordinator drives.
            slots_probe=app.state.local_gateway.slots,
            # The disk layer for the agent-turn prefix: restores jerv's saved prompt cache
            # in ~2 s where a turn (or the keeper's prime) would otherwise pay a ~60 s
            # prefill. Shares the models volume with the weights (jbrain.llm.kv_prefix).
            kv_prefix=app.state.kv_prefix,
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
        # Sibling emitter for numeric/bool read-aloud config (answer_speed/pitch/chorus), so the
        # wall display reflects the owner's chosen voice effects live, without a redeploy.
        app.state.brain_value_emit = build_value_emitter(settings.brain_events_url)
        # The wall base URL (events URL minus /event) — kept for any wall-direct call.
        app.state.brain_base_url = settings.brain_events_url.removesuffix("/event")
        # TTS moved into the `tts-stt` service: the authenticated /api/brain/tts +
        # /api/brain/voices proxy reaches its Kokoro renderer here (so the PWA read-aloud +
        # voice picker never touch an unauthenticated service directly), and the tts_debug
        # flag is pushed to its /event (not the wall's).
        tts_base = settings.brain_tts_url.rstrip("/")
        app.state.brain_tts_base_url = tts_base
        app.state.brain_tts_flag_emit = build_flag_emitter(f"{tts_base}/event" if tts_base else "")

        # The hosted Tavily Extract tier reads its toggle + key LIVE from app.settings on each
        # fetch (SYSTEM_CTX owner session), the stored key taking precedence over the env
        # fallback — so the PWA Settings panel is the live control surface with no restart,
        # exactly like the LLM router's live overrides (docs/plans/TAVILY_FETCH_TIER_PLAN.md).
        async def _tavily_settings() -> tuple[bool, str]:
            enabled = await settings_store.tavily_enabled(SYSTEM_CTX)
            key = await settings_store.tavily_api_key(SYSTEM_CTX) or settings.tavily_api_key
            return enabled, key

        # The per-domain fetch-health store (app.blocked_domains) also backs the LEARNED
        # Tavily-first routing: when byparr genuinely misses but Tavily recovers a page, the
        # fetcher records the domain (`record_solver_failed`) so a future fetch routes it straight
        # to Tavily (`tavily_first_hosts`), skipping the doomed on-box legs. Created here (before
        # the fetcher) so those two thin callbacks can be injected; also shared on app.state below
        # for the 24h paywall/bot-wall skip list the web handlers consult.
        domain_skips = DomainSkipRepo(maker)
        web_fetcher = WebFetcher(
            reader_url=settings.reader_url,
            solver_url=settings.solver_url,
            solver_first_domains=settings.solver_first_domains,
            tavily_url=settings.tavily_url,
            tavily_extract_depth=settings.tavily_extract_depth,
            tavily_settings=_tavily_settings,
            tavily_first_hosts=domain_skips.tavily_first_hosts,
            record_solver_failed=domain_skips.record_solver_failed,
        )
        searxng = SearxngClient(settings.searxng_url)
        # Curated per-category RSS/Atom feeds backing jerv's `news_feed` tool
        # (docs/plans/NEWS_FEED_PLAN.md). Fetches feed bytes through the shared SSRF-guarded
        # web_fetcher (all egress in one place) and parses them offline; the pinned feed map
        # comes from config, never the model.
        news_feeds = FeedClient(web_fetcher, settings.news_feeds)
        # Shared on app.state so the jcode search bridge (api.jcode_llm web_search /
        # web_fetch) reaches the SAME cached instances jerv uses. The sandbox can't touch
        # searxng directly (it's on `internal`, the sandbox on `jcode`), so this api — the
        # one peer on both networks — is its only path (docs/plans/JCODE_GROK_INTERNET_PLAN.md).
        app.state.searxng = searxng
        app.state.web_fetcher = web_fetcher
        # The deterministic OCR sidecar client (docs/plans/RAPIDOCR_PLAN.md): shared on
        # app.state so the ingest cross-validation, the jerv `ocr` tool, and the jcode `ocr`
        # bridge all reach the one pinned instance. Empty url ⇒ degrade to VLM-only OCR.
        app.state.rapidocr = RapidOcrClient(settings.rapidocr_url)
        # The HTML -> PNG renderer (docs/plans/AGENT_CANVAS_PLAN.md §3b): shared on
        # app.state so the canvas `html` op — and any later tool wanting a flowchart,
        # table, or report card — reaches the one pinned, egress-free instance rather
        # than shipping model-authored markup to the PWA. Empty url ⇒ the html lane
        # reports unavailable and the shape ops keep working.
        app.state.htmlrender = HtmlRenderClient(settings.htmlrender_url)
        # A YouTube URL through web_fetch reads as a lightweight title+channel+description+
        # captions view (jbrain.web.youtube) — no media download or GPU, unlike analyze_video.
        # Bound to the tested yt-dlp resolver + caption fetcher; the blocking resolve runs off
        # the loop. Gated on yt-dlp: a stripped env falls back to a normal HTML fetch.
        youtube_fetch = (
            functools.partial(
                youtube_page,
                resolver=resolve_stream,
                caption_fetcher=fetch_caption_transcript,
                run_blocking=asyncio.to_thread,
            )
            if ytdlp_available()
            else None
        )
        # Cross-turn tool-result store (docs/plans/CROSS_TURN_TOOL_RESULTS_PLAN.md): a fetched
        # page's full text is persisted (heavy text in the blob store, metadata + paging cursor
        # in the row) so a follow-up turn re-reads/continues it via read_artifact instead of a
        # network re-fetch. Its own AgentSessionRepo (app.state.agent_sessions is built later).
        app.state.tool_artifacts = ToolArtifactRepo(maker, AgentSessionRepo(maker))
        # The 24h paywall/bot-wall skip list (docs/archive/DOMAIN_HEALTH_PLAN.md): global SYSTEM
        # reference data (app.blocked_domains), so it needs only the sessionmaker — it reads and
        # records under SYSTEM_CTX. web_fetch short-circuits a listed host and records a fresh
        # persistent block; web_search drops listed hosts from its results.
        app.state.domain_skips = domain_skips
        web_handlers = build_web_handlers(
            searxng,
            web_fetcher,
            emit=brain_emit,
            youtube=youtube_fetch,
            artifacts=app.state.tool_artifacts,
            blobs=app.state.blob_store,
            domain_skips=app.state.domain_skips,
            feeds=news_feeds,
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
        # jerv's Grokipedia tools (GROKIPEDIA_TOOL_PLAN.md) — search/outline/section/
        # citations/related over xAI's encyclopedia. Open-internet only (no xAI key):
        # API-first via Grokipedia's own endpoints, SSR-HTML fallback. Merged into the web
        # handlers so they ride the existing `web` permission gate, the same sandboxed-web
        # posture as web_search; a browser UA + cookie jar handle Cloudflare, per-slug cache
        # collapses a drill-down to one fetch.
        web_handlers.update(build_grokipedia_handlers(GrokipediaClient(), emit=brain_emit))
        # jerv's free public-records lookup (docs/reference/ASSISTANT.md "Agent selection") —
        # The public_records umbrella (docs/plans/TOOL_CATALOG_PLAN.md): ONE `web`-gated
        # tool fanning a name across four FREE, keyless sources — court (CourtListener
        # opinions + RECAP dockets + judges/officials alias lookup), identity (Wikidata
        # aliases/maiden/former names + occupation), license (NPPES NPI registry: license +
        # other_names), federal_register (agency debarments/enforcement). Base URLs pinned
        # from config, only a public name/term goes out; merged into the web handlers so it
        # rides the same `web` gate + sandboxed-web posture as web_search. Clients are shared
        # on app.state so the jcode bridge (like searxng/web_fetcher) reaches one instance each.
        app.state.courtlistener = CourtListenerClient(
            settings.courtlistener_url, settings.courtlistener_token
        )
        app.state.wikidata = WikidataClient(settings.wikidata_url)
        app.state.nppes = NppesClient(settings.nppes_url)
        app.state.federal_register = FederalRegisterClient(settings.federal_register_url)
        web_handlers.update(
            build_public_records_handlers(
                app.state.courtlistener,
                app.state.wikidata,
                app.state.nppes,
                app.state.federal_register,
                emit=brain_emit,
            )
        )
        # Portal-search resolvers (DYNAMIC_PORTAL_FETCH_PLAN.md): each pinned state portal is
        # constructed here (like the public-records clients) and passed to the one `portal_search`
        # tool, which reaches it through the shared SSRF-guarded web_fetcher. Adding a portal is a
        # new adapter + one construction line here. Always registered so the sidecar has its
        # handler; an unconfigured resolver reports "not configured".
        app.state.portal_resolvers = (
            FlSunbizResolver(settings.sunbiz_url),
            FlDfsResolver(settings.dfs_licensee_url),
        )
        web_handlers.update(
            build_portal_handlers(web_fetcher, app.state.portal_resolvers, emit=brain_emit)
        )
        web_handlers.update(
            build_weather_handlers(weather_client, app.state.city_geocoder, nws_client)
        )
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
        # Built before the registry: analyze_image resolves a chat attachment's bytes
        # through the same TurnAttachmentRepo, so it must exist first.
        app.state.agent_sessions = AgentSessionRepo(maker)
        app.state.turn_attachments = TurnAttachmentRepo(maker, app.state.agent_sessions)
        # The owner's local image stack (docs/archive/IMAGE_GEN_PLAN.md): the launcher's
        # render service plus jerv's read-only analyze_image sidecar. Wired only when a
        # host-managed ComfyUI is configured; None otherwise, so an unconfigured box
        # silently lacks the feature — the registry then drops the sidecar. The
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
            # The render core (Wave L2): the direct owner API (api/images_render) drives this
            # one path — image generation/editing lives in the Images launcher, not in an
            # agent tool. It owns the unified-memory time-share (free the LLM before /
            # ComfyUI after a render), the blob put, and the RLS-scoped row insert.
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
                # Lets a finished render drop the page-cache copy of the diffusion weights it
                # read; without it that residue reads as a full box to the memory budget.
                models_dir=settings.comfyui_models_dir,
            )
            image_handlers = build_image_handlers(
                app.state.blob_store,
                app.state.generated_image_repo,
                app.state.turn_attachments,
                maker,
                # Routes analyze_image's vision read (the `agent.vision` task) so a
                # text-only agent model can still see an image via a vision model.
                app.state.llm_router,
                # The deterministic OCR sidecar, used by analyze_image as a fast CPU
                # text-detector: it gates the verbatim vision-OCR pass so a document/screenshot
                # comes back with its exact text appended, and a text-less photo doesn't.
                rapidocr=app.state.rapidocr,
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
        # The job launcher (docs/archive/JLAUNCH_PLAN.md): the api proxies its control surface
        # and streams its artifact into the blob store at share time. None (empty url) =
        # fail-closed: the /jlaunch routes 404 and the launcher tile is hidden.
        app.state.jlaunch_client = (
            JlaunchClient(settings.jlaunch_url, settings.jlaunch_token)
            if settings.jlaunch_url
            else None
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
        # jerv's deterministic OCR read (docs/plans/RAPIDOCR_PLAN.md) — the verbatim
        # counterpart to analyze_image, over the on-box RapidOCR sidecar.
        ocr_handlers = build_ocr_handlers(
            app.state.rapidocr, app.state.blob_store, app.state.turn_attachments
        )
        # jerv's standalone HTML render (AGENT_CANVAS_PLAN §3b): model-authored markup
        # rasterized by the same egress-free sidecar the canvas `html` op uses, shown as
        # an image card. Wired only when the sidecar is configured, so a box without one
        # drops the tool from the registry rather than offering a render it cannot do.
        html_handlers = (
            build_html_handlers(
                maker,
                app.state.blob_store,
                app.state.generated_image_repo,
                app.state.llm_router,
                app.state.htmlrender,
            )
            if app.state.htmlrender.configured
            else None
        )
        # jerv's canvas (docs/plans/AGENT_CANVAS_PLAN.md): mark up the owner's photo,
        # or sketch on a blank sheet, through a retained scene the model edits by id.
        # The `html` op renders through the egress-free htmlrender sidecar; with no
        # sidecar configured the shape ops still work and the block reports why.
        canvas_handlers = build_canvas_handlers(
            maker,
            app.state.blob_store,
            app.state.tool_artifacts,
            app.state.generated_image_repo,
            app.state.turn_attachments,
            app.state.llm_router,
            app.state.htmlrender,
        )
        # jerv's crop lane (AGENT_CANVAS_PLAN W4): cut N regions out of one image and
        # return them as one image_set card. Model-gated with the canvas pair — it
        # grounds regions with the vision model, so an unqualified coordinate base
        # would cut confidently wrong crops.
        crop_handlers = build_crop_handlers(
            maker,
            app.state.blob_store,
            app.state.generated_image_repo,
            app.state.turn_attachments,
            app.state.llm_router,
            app.state.rapidocr,
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
            embed=app.state.embed_client,
            # The SAME FeedClient the news_feed tool uses, so the deep-research engine's Wave-B
            # feed pre-pull and a scout's news_feed call share one cache (NEWS_FEED_PLAN.md).
            feeds=news_feeds,
            # SearXNG + the URL fetcher back the `engine: briefing` deterministic-gather builder
            # (DAILY_NEWS_V2_PLAN.md) — the same instances the web tools use.
            searxng=searxng,
            fetcher=web_fetcher,
            # The free, keyless public-records clients backing the deterministic pre-gather
            # (CANDIDATE_PROFILE_V2_PLAN.md) — the SAME instances the `public_records` tool uses.
            wikidata=app.state.wikidata,
            courtlistener=app.state.courtlistener,
            nppes=app.state.nppes,
            federal_register=app.state.federal_register,
            image_handlers=image_handlers,
            transcribe_handlers=transcribe_handlers,
            video_handlers=video_handlers,
            stream_handlers=stream_handlers,
            grab_handlers=grab_handlers,
            fetch_image_handlers=fetch_image_handlers,
            compare_handlers=compare_handlers,
            ocr_handlers=ocr_handlers,
            html_handlers=html_handlers,
            canvas_handlers=canvas_handlers,
            crop_handlers=crop_handlers,
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
        # Close any agent/subagent run rows a prior process left 'running' — a crash/OOM/
        # SIGKILL, or a shutdown drain that overran its bound, never runs the finally that
        # settles the row. This fresh process owns none yet, so every such row is a
        # pre-restart orphan that would otherwise read 'active' forever on the Runs surface
        # (and let a rejoining PWA mistake a dead run for a live one). Awaited before serving
        # so no request sees the backlog; best-effort — a reap hiccup must never block boot.
        with suppress(Exception):
            boot_reaped = await app.state.agent_runlog.reap_stranded(SYSTEM_CTX)
            if boot_reaped:
                structlog.get_logger().info("agent.runlog.boot_reaped", reaped=boot_reaped)
        # Bound accumulation while the process stays up (a child stranded by a rare
        # double-cancel): an age-based sweep above the hard turn wall-clock, so it never
        # races a genuinely-live detached turn.
        stranded_reaper_task = asyncio.create_task(
            reap_stranded_loop(app.state.agent_runlog, SYSTEM_CTX)
        )
        # The vitals graph's past: GPU load sampled once a second into memory, so the
        # detail screen opens with a history instead of an empty plot after a reload.
        # Runs whether or not anyone is watching — that is what makes it a history.
        app.state.vitals_ring = VitalsRing()
        # The other half of "what is the box doing this second": the in-flight model load,
        # answered once per second for the whole box rather than once per client per
        # surface. Created here rather than on first use so two frames landing together at
        # startup cannot each build one (jbrain.api.ops._LoadProbe).
        app.state.load_probe = ops._LoadProbe()
        # Off under test. The sampler writes a reading a second for the life of the
        # process, so a test that seeds the ring and then asserts on it races a tick that
        # overwrites the seed with whatever a CI container's absent gauge reads (None).
        # That is a genuine flake, not a hypothetical: it surfaced the moment the suite was
        # run under enough parallelism to make the window likely.
        vitals_sampler_task = (
            asyncio.create_task(sample_loop(app.state.vitals_ring))
            if settings.vitals_sampler_enabled
            else None
        )
        app.state.run_reader = RunLogReader(maker)
        # The owner-facing Research Library reader: browse/search/delete over jerv's
        # persisted deep-research reports + analysed videos (the external corpus).
        app.state.research_library = ResearchLibrary(
            maker, app.state.embed_client, app.state.blob_store
        )
        # Per-IP bound on the unauthenticated report-share reads (api/research_share.py).
        # Generous enough to browse a folder's reports back-to-back; a scanner backs off.
        app.state.research_share_rate_limiter = TokenBucket(capacity=30, refill_per_sec=1.0)
        # Per-IP bound on the unauthenticated jlaunch results-share reads (api/jlaunch_share.py).
        app.state.jlaunch_share_rate_limiter = TokenBucket(capacity=30, refill_per_sec=1.0)
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
        # Plan auto-continuation (JERV_PLANNING_TOOL_PLAN.md): the web-process sweep that
        # fires due plan continuations as headless answer-only jerv turns. Reuses the same
        # engine /chat and tasks use, and the live-turns registry (so it never stacks on a
        # live turn). Its own LoopTurnExecutor over the shared router + agent registry.
        app.state.plan_continuation_runner = PlanContinuationRunner(
            maker=maker,
            executor=LoopTurnExecutor(app.state.llm_router, app.state.agent_registry),
            runlog=app.state.agent_runlog,
            transcript=app.state.agent_transcript,
            live_turns=app.state.live_turns,
            owner_principal_id=lambda: _owner_principal_id(maker),
            turn_starting=app.state.turn_starting,
            client_presence=app.state.plan_presence,
            max_concurrent=agent._MAX_CONCURRENT_TURNS,
            # Persists the continuation turn's context fill so the meter restores on reopen.
            sessions=app.state.agent_sessions,
        )
        plan_continuation_task = asyncio.create_task(
            run_plan_continuation_loop(app.state.plan_continuation_runner)
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
        # Re-stamp llama-swap.yaml with the operator's SAVED per-model context-window/slot
        # overrides BEFORE the warm keeper primes. A deploy's config re-stamp
        # (deploy/local-models-sync.sh → llama_swap_config._main) regenerates from the BASE
        # catalog and drops these overrides, so a model whose saved window exceeds its base
        # (Nemotron 3.5 Lightning: 500k over a 32k base) would otherwise reload at 32k and every
        # agent turn — whose own system+tools prefix is ~33k tokens — would overflow. Idempotent
        # and best-effort: a no-op when the config already matches, so a plain restart keeps its
        # warm model. Awaited (not detached) so the config is correct before the prime below.
        with suppress(Exception):
            await llm_settings_api.reconcile_gateway_windows_on_boot(
                settings, settings_store, app.state.local_gateway, SYSTEM_CTX
            )
        # Keep the interactive model (agent.turn, when it routes local) resident AND primed
        # so the first jerv message after a restart/update is instant, not a cold weight-load
        # + persona+tools prefill. Nothing else does this on boot: schedule_restore only undoes
        # same-process displacements, and an on-demand load is bare. Detached + best-effort;
        # reconciles on boot and on an interval, so it also self-heals a gateway-only restart.
        app.state.warm_keeper = WarmKeeper(
            gateway=app.state.local_gateway,
            registry=app.state.agent_registry,
            router=app.state.llm_router,
            hold_loader=lambda: settings_store.code_mode_hold_names(SYSTEM_CTX),
            # The same operator switch the coordinator reads. The keeper is the OTHER auto-load
            # path on this box, and without this it reloaded the primary model every 5s no
            # matter what the setting said — so turning auto-reload off stopped restores while
            # a 68 GiB model kept coming straight back, which is not what the switch claims.
            auto_restore_loader=lambda: settings_store.llm_local_auto_restore(SYSTEM_CTX),
            # Restore-before-prime and save-after-prime: the same store the router uses
            # inline, so a boot re-prime is ~1 s off a saved slot instead of a ~60 s prefill.
            kv_prefix=app.state.kv_prefix,
        )
        warm_keeper_task = asyncio.create_task(app.state.warm_keeper.run())
        # Stopping a service is a synchronous `docker stop` on the supervisor — up to
        # the container's SIGTERM grace (ComfyUI's ~10 s) before it returns — so the
        # default 5 s httpx timeout would spuriously fail a stop that actually succeeds.
        app.state.supervisor_client = httpx.AsyncClient(
            base_url=settings.supervisor_url, timeout=30.0
        )
        yield
        warm_keeper_task.cancel()
        if live_task is not None:
            live_task.cancel()
        tasks_loop_task.cancel()
        plan_continuation_task.cancel()
        intake_reaper_task.cancel()
        stranded_reaper_task.cancel()
        if vitals_sampler_task is not None:
            vitals_sampler_task.cancel()
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
        # Unwire the box-event writer BEFORE the engine goes: it is a module global, so a
        # late narration (a load cancelled during shutdown) would otherwise open a session
        # on a disposed engine.
        box_events.reset()
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
    # The job launcher: owner REST proxy + run mirror + share mint, the owner terminal WS,
    # and the public results/download surface (no owner dep). All owner-gated except the
    # public jlaunch_share reads, which are token-in-path + rate-limited + noindex.
    app.include_router(jlaunch.router, prefix="/api")
    app.include_router(jlaunch_terminal.router, prefix="/api")
    app.include_router(jlaunch_share.router, prefix="/api")
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
    app.include_router(plans.router, prefix="/api")
    app.include_router(proposals.router, prefix="/api")
    app.include_router(research_library.router, prefix="/api")
    app.include_router(research_share.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(session_bridge.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(gmail_settings_api.router, prefix="/api")
    app.include_router(tavily_settings_api.router, prefix="/api")
    app.include_router(tasks_api.router, prefix="/api")
    app.include_router(tiles.router, prefix="/api")
    app.include_router(wiki.router, prefix="/api")
    return app


app = create_app()
