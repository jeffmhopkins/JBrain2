"""The JPet drives tick against real Postgres (docs/plans/JPET_PLAN.md §4).

Proves the exit criterion of W0: the pet exists and its drives advance on a clock —
`jpet_tick` creates the pet once (idempotently) and each tick decays the needs by
the elapsed time, with mood recomputed. No LLM is involved.
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


async def test_jpet_tick_creates_the_pet(maker: async_sessionmaker) -> None:
    await _owner_ctx(maker)
    info = await jpet_tick(maker, SqlJpetRepo(maker))
    assert info is not None
    assert info.name == "Blink"
    assert info.domain == "general"


async def test_ensure_pet_is_idempotent(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    first = await repo.ensure_pet(ctx, name="Blink", domain="general")
    second = await repo.ensure_pet(ctx, name="Blink", domain="general")
    assert first.id == second.id


async def test_tick_decays_drives_over_elapsed_time(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    # Pin a known starting state so the assertion is exact regardless of prior ticks.
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.pet_state SET food=80, energy=80, fun=70, love=70,"
                " asleep=false, last_tick_at=:base WHERE domain_code='general'"
            ),
            {"base": base},
        )

    ticked = await repo.tick(ctx, domain="general", now=base + timedelta(hours=5))
    assert ticked is not None
    # Awake for 5h: food -6/h, energy -4/h — dropped but not bottomed out.
    assert ticked.drives.food == pytest.approx(50.0)
    assert ticked.drives.energy == pytest.approx(60.0)
    assert 0 < ticked.drives.food < 80
    assert ticked.mood == "neutral"  # average 57.5 → neutral band
