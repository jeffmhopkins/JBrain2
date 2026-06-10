from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.api import auth, health, notes, ops, search
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings, get_settings
from jbrain.embed import TeiEmbedClient
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import PgJobQueue
from jbrain.search.repo import SqlSearchRepo
from jbrain.search.service import SearchService
from jbrain.storage import FsBlobStore

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
        app.state.notes_repo = SqlNotesRepo(maker)
        app.state.blob_store = FsBlobStore(settings.blob_dir)
        app.state.job_queue = PgJobQueue(maker)
        app.state.search_service = SearchService(
            SqlSearchRepo(maker), TeiEmbedClient(settings.embed_url)
        )
        app.state.supervisor_client = httpx.AsyncClient(base_url=settings.supervisor_url)
        yield
        await app.state.supervisor_client.aclose()
        await engine.dispose()

    app = FastAPI(title="JBrain", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(notes.router, prefix="/api")
    app.include_router(ops.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    return app


app = create_app()
