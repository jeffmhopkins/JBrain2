"""JPet commands + the sync contract against real Postgres (docs/plans/JPET_PLAN.md W1).

Proves a command mutates the authoritative row (feed fills, move sets the floor
target) and that the resulting state — the thing the API publishes — reaches a
broadcaster subscriber, i.e. one client's command updates every other surface live.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.jpet.broadcast import PetBroadcaster
from jbrain.jpet.repo import SqlJpetRepo
from jbrain.jpet.service import Command
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


async def _pin(maker: async_sessionmaker, ctx: SessionContext) -> None:
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "UPDATE app.pet_state SET food=50, energy=80, fun=70, love=70,"
                " asleep=false, target_x=0, target_z=0 WHERE domain_code='general'"
            )
        )


async def test_feed_command_fills_food(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    await _pin(maker, ctx)
    info = await repo.apply_command(ctx, domain="general", command=Command("feed"))
    assert info is not None
    assert info.drives.food == 76.0  # 50 + 26
    assert info.action == "eat"


async def test_move_command_sets_target(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    info = await repo.apply_command(ctx, domain="general", command=Command("move", x=0.5, z=-0.3))
    assert info is not None
    assert info.action == "walk"
    assert info.target_x == pytest.approx(0.5)
    assert info.target_z == pytest.approx(-0.3)


async def test_apply_reply_persists_speech_and_emotion(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    info = await repo.apply_reply(
        ctx, domain="general", speech="Boo! Hallo!", emotion="excited", action="play"
    )
    assert info is not None
    assert info.speech == "Boo! Hallo!"
    assert info.emotion == "excited"
    assert info.action == "play"


async def test_memory_records_and_reads_newest_first(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    await repo.record_memory(ctx, domain="general", kind="said", body="Emma fed you an apple")
    await repo.record_memory(ctx, domain="general", kind="said", body="Sam played fetch")
    recent = await repo.recent_memories(ctx, domain="general", limit=6)
    assert recent[0] == "Sam played fetch"  # newest first
    assert "Emma fed you an apple" in recent


async def test_set_target_makes_the_pet_walk(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    info = await repo.set_target(ctx, domain="general", x=-0.6, z=0.4)
    assert info is not None
    assert info.target_x == pytest.approx(-0.6)
    assert info.target_z == pytest.approx(0.4)
    assert info.action == "walk"


async def test_command_state_reaches_a_subscriber(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    await _pin(maker, ctx)

    # A subscriber (a Wall/phone stream) is listening; a command from "another client"
    # is applied and its resulting state published — the subscriber must see it.
    broadcaster = PetBroadcaster()
    queue = broadcaster.subscribe()
    info = await repo.apply_command(ctx, domain="general", command=Command("feed"))
    assert info is not None
    broadcaster.publish(info)
    received = await queue.get()
    assert received.drives.food == 76.0
    assert received.action == "eat"
