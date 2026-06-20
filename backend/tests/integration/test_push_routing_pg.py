"""View-scope-aware poke routing against real Postgres (JBrain360 M6b).

A crossing for subject X pokes the members who may SEE X — its family group, not X
itself and not an outsider — and only their ACTIVE device tokens, de-duplicated. The
routing query (`app.visible_subjects`) and the active-token read are the firewall.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import device_context, scoped_session
from jbrain.push import PushRouter, SqlFcmTokenRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class RecordingNotifier:
    def __init__(self) -> None:
        self.poked: list[list[str]] = []

    async def poke(self, tokens: list[str]) -> None:
        self.poked.append(tokens)


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


async def _group(maker: async_sessionmaker, members: list[str]) -> None:
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text("INSERT INTO app.family_group (name) VALUES ('family') RETURNING id")
            )
        ).scalar()
        for sid in members:
            await session.execute(
                text("INSERT INTO app.view_scope (group_id, member_subject_id) VALUES (:g, :s)"),
                {"g": str(gid), "s": sid},
            )


async def test_routes_a_crossing_to_co_members_only(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    pid_c, sid_c = await _device(maker, "Carol")  # outsider, not in the group
    await _group(maker, [sid_a, sid_b])
    tokens = SqlFcmTokenRepo(maker)
    await tokens.register(
        device_context(pid_a, sid_a), principal_id=pid_a, subject_id=sid_a, token=f"a-{pid_a}"
    )
    await tokens.register(
        device_context(pid_b, sid_b), principal_id=pid_b, subject_id=sid_b, token=f"b-{pid_b}"
    )
    await tokens.register(
        device_context(pid_c, sid_c), principal_id=pid_c, subject_id=sid_c, token=f"c-{pid_c}"
    )

    notifier = RecordingNotifier()
    await PushRouter(maker, tokens).poke_viewers_of(notifier, sid_a)

    # Alice's crossing pokes co-member Bob — not Alice herself, not outsider Carol.
    assert notifier.poked == [[f"b-{pid_b}"]]


async def test_a_solo_subject_pokes_no_one(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")  # no group
    tokens = SqlFcmTokenRepo(maker)
    await tokens.register(
        device_context(pid_a, sid_a), principal_id=pid_a, subject_id=sid_a, token=f"a-{pid_a}"
    )

    notifier = RecordingNotifier()
    await PushRouter(maker, tokens).poke_viewers_of(notifier, sid_a)
    assert notifier.poked == []  # no co-members → notifier never called


async def test_revoked_co_member_drops_out_of_routing(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "Alice")
    pid_b, sid_b = await _device(maker, "Bob")
    await _group(maker, [sid_a, sid_b])
    tokens = SqlFcmTokenRepo(maker)
    await tokens.register(
        device_context(pid_a, sid_a), principal_id=pid_a, subject_id=sid_a, token=f"a-{pid_a}"
    )
    await tokens.register(
        device_context(pid_b, sid_b), principal_id=pid_b, subject_id=sid_b, token=f"b-{pid_b}"
    )

    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.principals SET revoked_at = now() WHERE id = cast(:p AS uuid)"),
            {"p": pid_b},
        )

    notifier = RecordingNotifier()
    await PushRouter(maker, tokens).poke_viewers_of(notifier, sid_a)
    # Bob's only device is revoked → no live tokens → no poke at all.
    assert notifier.poked == []
