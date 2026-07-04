"""The JPet ensure loop against real Postgres (docs/plans/JPET_V3_PLAN.md).

Proves the server guarantees the pet exists — `jpet_tick` creates it once (idempotently,
with the room seeded) and each tick just refreshes the `last_tick_at` heartbeat (v3 has no
drives; the pet's life runs on the wall). No LLM is involved.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.scheduler import jpet_tick
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


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_jpet_tick_creates_the_pet_and_seeds_the_room(maker: async_sessionmaker) -> None:
    await _owner_ctx(maker)
    info = await jpet_tick(maker, SqlJpetRepo(maker))
    assert info is not None
    assert info.name == "Blink"
    assert info.domain == "general"
    assert "ball" in info.objects and "bed" in info.objects  # room seeded on create


async def test_ensure_pet_is_idempotent(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    first = await repo.ensure_pet(ctx, name="Blink", domain="general")
    second = await repo.ensure_pet(ctx, name="Blink", domain="general")
    assert first.id == second.id


async def test_tick_refreshes_the_heartbeat(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("UPDATE app.pet_state SET last_tick_at=:base WHERE domain_code='general'"),
            {"base": base},
        )

    when = base + timedelta(hours=5)
    ticked = await repo.tick(ctx, domain="general", now=when)
    assert ticked is not None
    assert ticked.last_tick_at == when  # the heartbeat advanced; no drives to change
