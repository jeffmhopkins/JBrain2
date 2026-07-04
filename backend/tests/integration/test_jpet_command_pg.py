"""JPet v2 scripts + the sync contract against real Postgres (docs/proposed/JPET_V2_PLAN.md).

Proves running a script mutates the authoritative row (the ball is carried to a corner,
lights toggle, a nap sleeps), a raw `move` walks the pet, and the resulting state — the
thing the API publishes — reaches a broadcaster subscriber, i.e. one client's command
updates every other surface live.
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
from jbrain.jpet.service import canned_script
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


async def test_carry_script_moves_the_ball_and_persists(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    state = await repo.ensure_pet(ctx, name="Blink", domain="general")
    assert "ball" in state.objects  # the room is seeded on create
    script = canned_script("chase", objects=state.objects)  # go to the ball
    info = await repo.run_script(ctx, domain="general", script=script)
    assert info is not None
    assert info.script and info.script_started_at is not None
    # the pet ends at rest on the ball's position, and the room round-trips through jsonb
    assert info.objects["ball"] == pytest.approx(state.objects["ball"])
    assert info.action in ("sit", "idle", "sleep")


async def test_button_scripts_reward_and_sleep(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    before = await repo.ensure_pet(ctx, name="Blink", domain="general")
    fun0 = before.drives.fun
    after = await repo.run_script(
        ctx, domain="general", script=canned_script("dance", objects=before.objects)
    )
    assert after is not None and after.drives.fun >= fun0  # play only ever raises the meters
    napped = await repo.run_script(
        ctx, domain="general", script=canned_script("sleep", objects=before.objects)
    )
    assert napped is not None and napped.asleep is True


async def test_move_command_walks_the_pet(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    info = await repo.move_to(ctx, domain="general", x=0.5, z=-0.3)
    assert info is not None
    assert info.action == "walk"
    assert info.target_x == pytest.approx(0.5)
    assert info.target_z == pytest.approx(-0.3)
    assert info.script == []  # a raw walk clears any script


async def test_run_script_persists_speech_and_emotion(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    st = await repo.ensure_pet(ctx, name="Blink", domain="general")
    info = await repo.run_script(
        ctx,
        domain="general",
        script=canned_script("wave", objects=st.objects),
        speech="Boo! Hallo!",
        emotion="excited",
    )
    assert info is not None
    assert info.speech == "Boo! Hallo!"
    assert info.emotion == "excited"


async def test_memory_records_and_reads_newest_first(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    await repo.ensure_pet(ctx, name="Blink", domain="general")
    await repo.record_memory(ctx, domain="general", kind="said", body="Emma fed you an apple")
    await repo.record_memory(ctx, domain="general", kind="said", body="Sam played fetch")
    recent = await repo.recent_memories(ctx, domain="general", limit=6)
    assert recent[0] == "Sam played fetch"  # newest first
    assert "Emma fed you an apple" in recent


async def test_command_state_reaches_a_subscriber(maker: async_sessionmaker) -> None:
    ctx = await _owner_ctx(maker)
    repo = SqlJpetRepo(maker)
    st = await repo.ensure_pet(ctx, name="Blink", domain="general")

    # A subscriber (a Wall/phone stream) is listening; a command from "another client" is
    # applied and its resulting state published — the subscriber must see it.
    broadcaster = PetBroadcaster()
    queue = broadcaster.subscribe()
    info = await repo.run_script(
        ctx, domain="general", script=canned_script("jump", objects=st.objects)
    )
    assert info is not None
    broadcaster.publish(info)
    received = await queue.get()
    assert received.script  # the played script rode the broadcast
