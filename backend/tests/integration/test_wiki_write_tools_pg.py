"""add_source_exclusion's real write against Postgres: it inserts an owner+domain-scoped
wiki_source_exclusions row (RLS) and queues a rebuild of the affected article."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.loop import ToolContext
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, ctx, kind, payload, **kw) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_add_source_exclusion_writes_and_queues_rebuild(maker: async_sessionmaker) -> None:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    note_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i, :c, 'general', 'b')"
            ),
            {"i": note_id, "c": note_id[:12]},
        )
    jobs = FakeJobs()
    handlers = build_wiki_write_handlers(object(), jobs, maker)  # type: ignore[arg-type]
    ctx = ToolContext(session=OWNER, scopes=("general",))
    # A global exclusion (no article_id) → target 'all'; exercises the write + the rebuild enqueue.
    out = await handlers["add_source_exclusion"](
        {"note_id": note_id, "domain": "general", "reason": "noisy"}, ctx
    )
    assert "Excluded" in out
    async with scoped_session(maker, OWNER) as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM app.wiki_source_exclusions WHERE note_id = :n"),
                {"n": note_id},
            )
        ).scalar()
    assert count == 1
    assert jobs.enqueued and jobs.enqueued[0][0] == "wiki_rebuild"
