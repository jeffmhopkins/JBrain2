"""Owner family-membership management against real Postgres (JBrain360 M7a).

Proves the management actually drives the firewall: adding two subjects to the
family makes `viewer_may_see` true between them; removing one ends it. And the
`view_scope`/`family_group` write RLS is owner-only — a device context manages
nothing.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import device_context, scoped_session
from jbrain.family import SqlFamilyRepo
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


async def _subject(maker: async_sessionmaker, name: str) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'device')"),
            {"s": sid, "n": name},
        )
    return sid


async def _may_see(maker: async_sessionmaker, viewer: str, target: str) -> bool:
    async with scoped_session(maker, OWNER) as session:
        return bool(
            (
                await session.execute(
                    text("SELECT app.viewer_may_see(:v, :t)"), {"v": viewer, "t": target}
                )
            ).scalar()
        )


async def test_add_then_remove_toggles_family_visibility(maker: async_sessionmaker) -> None:
    alice = await _subject(maker, "Alice")
    bob = await _subject(maker, "Bob")
    repo = SqlFamilyRepo(maker)

    # Strangers before any membership.
    assert await _may_see(maker, alice, bob) is False

    await repo.add_member(OWNER, alice)
    await repo.add_member(OWNER, bob)
    # Now co-members — mutual visibility is on.
    assert await _may_see(maker, alice, bob) is True
    assert await _may_see(maker, bob, alice) is True
    assert {m.subject_id for m in await repo.members(OWNER)} >= {alice, bob}

    # Removing Bob ends his read path immediately.
    await repo.remove_member(OWNER, bob)
    assert await _may_see(maker, alice, bob) is False
    assert bob not in {m.subject_id for m in await repo.members(OWNER)}


async def test_membership_writes_are_owner_only(maker: async_sessionmaker) -> None:
    alice = await _subject(maker, "Alice")
    # A device context (non-owner) cannot create a family group — owner-only RLS.
    dev = device_context(str(uuid.uuid4()), alice)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, dev) as session:
            await session.execute(text("INSERT INTO app.family_group (name) VALUES ('intruder')"))
    # And it reads no groups (owner-only SELECT) — get-or-create finds nothing.
    async with scoped_session(maker, dev) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.family_group"))).scalar()
    assert count == 0


async def test_add_member_is_idempotent(maker: async_sessionmaker) -> None:
    cara = await _subject(maker, "Cara")
    repo = SqlFamilyRepo(maker)
    await repo.add_member(OWNER, cara)
    await repo.add_member(OWNER, cara)  # second add is a no-op, not a duplicate/error
    assert len([m for m in await repo.members(OWNER) if m.subject_id == cara]) == 1


async def _device(maker: async_sessionmaker, subject_id: str) -> str:
    pid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :k)"
            ),
            {"p": pid, "s": subject_id, "k": uuid.uuid4().hex},
        )
    return pid


async def test_member_revoke_tombstones_the_device_and_drops_the_family(
    maker: async_sessionmaker,
) -> None:
    from jbrain.devices.repo import SqlDeviceRepo
    from jbrain.devices.service import revoke_device

    alice = await _subject(maker, "Alice")
    bob = await _subject(maker, "Bob")
    pid_bob = await _device(maker, bob)
    repo = SqlFamilyRepo(maker)
    await repo.add_member(OWNER, alice)
    await repo.add_member(OWNER, bob)
    assert await _may_see(maker, alice, bob) is True

    # Revoke Bob the member: tombstone the device principal + drop from family.
    await revoke_device(SqlDeviceRepo(maker), OWNER, bob)
    await repo.remove_member(OWNER, bob)

    # The principal is tombstoned (its dashboard cookie 401s, its MQTT auth/ACL deny)…
    async with scoped_session(maker, OWNER) as session:
        revoked_at = (
            await session.execute(
                text("SELECT revoked_at FROM app.principals WHERE id = cast(:p AS uuid)"),
                {"p": pid_bob},
            )
        ).scalar()
    assert revoked_at is not None
    # …and the family read path is gone.
    assert await _may_see(maker, alice, bob) is False
    assert bob not in {m.subject_id for m in await repo.members(OWNER)}
