"""Tier-A memory against real Postgres: the block read/ACE-edit revision chain and
episodic recall (the SQL the unit tests fake). RLS isolation lives in
test_agent_memory_rls.py; this covers the queries end to end."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.memory import MemoryRepo, MemoryService
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def test_block_write_read_and_ace_edit_revision(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    repo = MemoryRepo(maker)
    svc = MemoryService(repo, FakeEmbedder(), "embed-v1")  # type: ignore[arg-type]

    block_id = await repo.write_block(
        OWNER,
        principal_id=pid,
        domain="health",
        block_kind="self_semantic",
        body_md="- raw numbers first\n- terse",
    )
    # A targeted ACE delete supersedes the block with a new revision.
    new_id = await svc.edit(OWNER, block_id, "remove", target=1)
    assert new_id != block_id

    live = await repo.live_blocks(OWNER, "self_semantic")
    assert [b.id for b in live] == [new_id]  # only the new revision is live
    assert live[0].body_md == "- raw numbers first"
    assert live[0].revision == 2


async def test_recall_finds_an_appended_episode_by_fts(maker: async_sessionmaker) -> None:
    await _owner_principal(maker)
    repo = MemoryRepo(maker)
    svc = MemoryService(repo, FakeEmbedder(), "embed-v1")  # type: ignore[arg-type]
    marker = uuid.uuid4().hex[:8]

    eid = await repo.append_episode(
        OWNER,
        body=f"owner asked about cholesterol labs {marker}",
        domain_scopes=["health"],
        embedding=None,
        embedding_model=None,
    )
    hits = await svc.recall(OWNER, f"cholesterol {marker}", limit=5)
    assert eid in {h.id for h in hits}
    # Recall touched the episode (recency tracks use).
    async with scoped_session(maker, OWNER) as session:
        touched = (
            await session.execute(
                text("SELECT last_accessed_at > created_at FROM app.agent_episodes WHERE id = :id"),
                {"id": eid},
            )
        ).scalar()
    assert touched is True
