from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.agent.memory import MemoryRepo, MemoryService
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.readtools import build_registry
from jbrain.agent.runlog import AgentRunLog, RunLogReader
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.skills import SkillService, SkillsRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.api import (
    agent,
    analysis,
    auth,
    devices,
    feed,
    health,
    notes,
    ops,
    owntracks,
    proposals,
    runs,
    search,
    sessions,
    wiki,
)
from jbrain.api import (
    appointments as appointments_api,
)
from jbrain.api import lists as lists_api
from jbrain.api import llm_settings as llm_settings_api
from jbrain.api import settings as settings_api
from jbrain.appointments.repo import SqlAppointmentsRepo
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings, get_settings
from jbrain.connectors.base import ConnectorRegistry
from jbrain.connectors.medical import medical_connectors
from jbrain.connectors.repo import SqlConnectorCache
from jbrain.connectors.service import ConnectorService
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.embed import TeiEmbedClient
from jbrain.lists.repo import SqlListsRepo
from jbrain.llm import build_router
from jbrain.locations import SqlLocationRepo
from jbrain.locations.ratelimit import TokenBucket
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX, PgJobQueue
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import FsBackupShelf, FsBlobStore
from jbrain.usage import SqlUsageRecorder
from jbrain.wiki.actions import WIKI_SPECS
from jbrain.wiki.readstore import WikiReadStore
from jbrain.wiki.talkstore import WikiTalkStore
from jbrain.workflow.automations import AutomationsReader
from jbrain.workflow.evalaction import EVAL_RUN_SPEC
from jbrain.workflow.registry import ACTION_SPECS
from jbrain.workflow.registry import build_registry as build_action_registry
from jbrain.workflow.scheduler import (
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
        # Per-device ingest cap: 60 fixes/min sustained (burst 60). A flooding
        # device gets a 429 and backs off; normal move-mode never trips it.
        app.state.location_rate_limiter = TokenBucket(capacity=60, refill_per_sec=1.0)
        app.state.notes_repo = SqlNotesRepo(maker)
        app.state.lists_repo = SqlListsRepo(maker)
        app.state.appointments_repo = SqlAppointmentsRepo(maker)
        app.state.blob_store = FsBlobStore(settings.blob_dir)
        app.state.backup_shelf = FsBackupShelf(settings.backups_dir)
        app.state.job_queue = PgJobQueue(maker)
        # The action registry the emergency-trigger control resolves a sweep's
        # pipeline through (workflow/scheduler.fire_trigger) and the Automations
        # surface renders the Catalog from. Mirrors the worker's composed registry
        # EXACTLY — the shipped six plus every in-code action (purge, the three
        # reconcilers, the opt-in eval_run) — so any manual trigger fired from Ops
        # resolves to the same handler the scheduler would, and the Catalog lists
        # the full set the worker can run.
        action_registry = build_action_registry(
            (
                *ACTION_SPECS,
                PURGE_ACTION,
                RECONCILE_PENDING_NOTES_ACTION,
                RECONCILE_PENDING_INTEGRATION_ACTION,
                RECONCILE_UNEMBEDDED_NOTES_ACTION,
                EVAL_RUN_SPEC,
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
        # Any API-side LLM call must flow through this router so its tokens
        # land in app.llm_usage like the worker's do. The overrides loader reads
        # the live per-task routing/reasoning settings (SYSTEM_CTX owner session)
        # on each call so the settings screen takes effect without a restart.
        app.state.llm_router = build_router(
            settings,
            recorder=SqlUsageRecorder(maker),
            overrides_loader=lambda: settings_store.llm_task_overrides(SYSTEM_CTX),
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
            medical_connectors(settings.rxnav_url, settings.medlineplus_url)
        )
        app.state.connector_service = ConnectorService(connector_registry, SqlConnectorCache(maker))
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
        )
        app.state.agent_sessions = AgentSessionRepo(maker)
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
        app.state.agent_transcript = AgentTranscript(maker)
        app.state.supervisor_client = httpx.AsyncClient(base_url=settings.supervisor_url)
        yield
        await app.state.supervisor_client.aclose()
        await engine.dispose()

    app = FastAPI(title="JBrain", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    app.include_router(agent.router, prefix="/api")
    app.include_router(analysis.router, prefix="/api")
    app.include_router(appointments_api.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(devices.router, prefix="/api")
    app.include_router(feed.router, prefix="/api")
    app.include_router(lists_api.router, prefix="/api")
    app.include_router(llm_settings_api.router, prefix="/api")
    app.include_router(notes.router, prefix="/api")
    app.include_router(ops.router, prefix="/api")
    app.include_router(owntracks.router, prefix="/api")
    app.include_router(proposals.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(wiki.router, prefix="/api")
    return app


app = create_app()
