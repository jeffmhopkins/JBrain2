"""view_audit firewall + record_view against real Postgres (JBrain360 M3a, 0069).

Proves the who-saw-whom log: the owner records a view that the owner and the
*target* can read (who-saw-me), an unrelated device sees nothing, and a device can
attribute a view only to its own subject (no forging another's).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import device_context, scoped_session
from jbrain.locations import SqlLocationRepo
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


async def _device(maker: async_sessionmaker, name: str) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :k)"
            ),
            {"p": pid, "s": sid, "k": uuid.uuid4().hex},
        )
    return pid, sid


async def _count(maker: async_sessionmaker, ctx, sql: str, args: dict) -> int:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(sql), args)).scalar() or 0


async def test_owner_view_is_recorded_and_readable_by_owner_and_target(
    maker: async_sessionmaker,
) -> None:
    repo = SqlLocationRepo(maker)
    pid_t, sid_t = await _device(maker, "target")
    pid_x, sid_x = await _device(maker, "unrelated")

    await repo.record_view(
        OWNER,
        viewer_principal_id=str(uuid.uuid4()),
        viewer_subject_id="",  # the owner has no subject
        target_subject_id=sid_t,
        path="history",
    )

    on_target = "SELECT count(*) FROM app.view_audit WHERE target_subject_id = :t"
    # The owner sees it; the target reads the row about itself (who-saw-me).
    assert await _count(maker, OWNER, on_target, {"t": sid_t}) == 1
    assert await _count(maker, device_context(pid_t, sid_t), on_target, {"t": sid_t}) == 1
    # An unrelated device sees no audit rows at all.
    assert (
        await _count(maker, device_context(pid_x, sid_x), "SELECT count(*) FROM app.view_audit", {})
        == 0
    )


async def test_device_can_attribute_a_view_only_to_its_own_subject(
    maker: async_sessionmaker,
) -> None:
    repo = SqlLocationRepo(maker)
    pid_v, sid_v = await _device(maker, "viewer")
    _, sid_t = await _device(maker, "target")
    viewer = device_context(pid_v, sid_v)

    # The viewer device records its OWN view of the target, and reads it back.
    await repo.record_view(
        viewer,
        viewer_principal_id=pid_v,
        viewer_subject_id=sid_v,
        target_subject_id=sid_t,
        path="history",
    )
    assert (
        await _count(
            maker,
            viewer,
            "SELECT count(*) FROM app.view_audit WHERE viewer_subject_id = :v",
            {"v": sid_v},
        )
        == 1
    )

    # It cannot forge a view attributed to a different subject (WITH CHECK).
    with pytest.raises(ProgrammingError):
        await repo.record_view(
            viewer,
            viewer_principal_id=pid_v,
            viewer_subject_id=sid_t,  # not its own subject
            target_subject_id=sid_t,
            path="history",
        )
