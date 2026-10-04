"""The disk prompt cache's session exclusions on real Postgres: appends are one statement, so
concurrent re-scopes never drop each other's entry (a lost entry would let a chat that once read
a firewalled domain reach disk), and a junk value is never narrowed (FLASH_NEXT F4c)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.settings_store import LLM_KV_CONVERSATION_EXCLUDED_KEY, SqlSettingsStore
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


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_concurrent_exclusions_all_land_once(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = SqlSettingsStore(maker)
    await store.upsert(owner, LLM_KV_CONVERSATION_EXCLUDED_KEY, [])
    ids = [f"s-{i}" for i in range(24)]
    await asyncio.gather(*(store.exclude_llm_kv_conversation(owner, i) for i in ids * 2))
    stored = await store.get(owner, LLM_KV_CONVERSATION_EXCLUDED_KEY, None)
    assert sorted(stored) == sorted(ids), "every entry landed, none twice"
    assert await store.llm_kv_conversation_excluded(owner) == frozenset(ids)


async def test_a_junk_value_stays_everything_excluded(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    store = SqlSettingsStore(maker)
    await store.upsert(owner, LLM_KV_CONVERSATION_EXCLUDED_KEY, {"not": "a list"})
    await store.exclude_llm_kv_conversation(owner, "s-1")
    assert await store.get(owner, LLM_KV_CONVERSATION_EXCLUDED_KEY, None) == {"not": "a list"}
    assert await store.llm_kv_conversation_excluded(owner) == frozenset({"*"})
