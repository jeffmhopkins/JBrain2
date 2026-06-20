"""Device provisioning + device-key auth against real Postgres (Phase 7 Wave 2).

Proves the owner-only provisioning path (RLS), the full provision -> authenticate
-> rotate -> revoke lifecycle, and that a non-owner session cannot provision a
device (subjects/principals WITH CHECK is_owner).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys, service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices import service as device_service
from jbrain.devices.repo import SqlDeviceRepo
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


async def test_provision_authenticate_rotate_revoke_lifecycle(maker: async_sessionmaker) -> None:
    auth = SqlAuthRepo(maker)
    devices = SqlDeviceRepo(maker)

    provisioned = await device_service.provision_device(devices, OWNER, "Jeff's phone")
    # The returned key authenticates and resolves to the device's subject.
    principal = await service.authenticate_device(auth, provisioned.key)
    assert principal is not None
    assert principal.kind == "device_key"
    assert principal.subject_id == provisioned.device.id

    # It shows up in the owner's device list, active.
    listed = await devices.list(OWNER)
    assert [(d.id, d.revoked) for d in listed] == [(provisioned.device.id, False)]

    # Rotating issues a new working key and kills the old one.
    new_key = await device_service.rotate_device_key(devices, OWNER, provisioned.device.id)
    assert new_key is not None
    assert await service.authenticate_device(auth, provisioned.key) is None  # old key dead
    rotated = await service.authenticate_device(auth, new_key)
    assert rotated is not None and rotated.subject_id == provisioned.device.id

    # Revoking kills the current key and marks the device revoked.
    assert await device_service.revoke_device(devices, OWNER, provisioned.device.id) is True
    assert await service.authenticate_device(auth, new_key) is None
    assert (await devices.list(OWNER))[0].revoked is True


async def test_rename_relabels_subject_and_active_key(maker: async_sessionmaker) -> None:
    devices = SqlDeviceRepo(maker)
    provisioned = await device_service.provision_device(devices, OWNER, "phone")

    assert await device_service.rename_device(devices, OWNER, provisioned.device.id, "Jeff's phone")
    listed = await devices.list(OWNER)
    assert listed[0].label == "Jeff's phone"
    # The active key principal's label follows, so a re-pair config carries the name.
    async with scoped_session(maker, OWNER) as session:
        label = (
            await session.execute(
                text(
                    "SELECT label FROM app.principals"
                    " WHERE subject_id = :sid AND kind = 'device_key' AND revoked_at IS NULL"
                ),
                {"sid": provisioned.device.id},
            )
        ).scalar()
    assert label == "Jeff's phone"


async def test_delete_drops_the_device_its_keys_and_cascades_fixes(
    maker: async_sessionmaker,
) -> None:
    devices = SqlDeviceRepo(maker)
    auth = SqlAuthRepo(maker)
    provisioned = await device_service.provision_device(devices, OWNER, "phone")
    sid = provisioned.device.id

    # A stored fix hangs off the subject; delete must cascade it (ON DELETE CASCADE).
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (id, captured_at, subject_id, latitude, longitude)"
                " VALUES (:id, now(), :sid, 40.0, -74.0)"
            ),
            {"id": str(uuid.uuid4()), "sid": sid},
        )

    assert await device_service.delete_device(devices, OWNER, sid) is True
    # Gone from the list, its key no longer authenticates, and its fixes are cascaded
    # (the module shares one DB, so assert this device is absent, not an empty list).
    assert sid not in {d.id for d in await devices.list(OWNER)}
    assert await service.authenticate_device(auth, provisioned.key) is None
    async with scoped_session(maker, OWNER) as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM app.location_fixes WHERE subject_id = :sid"),
                {"sid": sid},
            )
        ).scalar()
    assert remaining == 0


async def test_rotate_revoke_rename_delete_unknown_device_are_noops(
    maker: async_sessionmaker,
) -> None:
    devices = SqlDeviceRepo(maker)
    missing = "00000000-0000-0000-0000-000000000000"
    assert await device_service.rotate_device_key(devices, OWNER, missing) is None
    assert await device_service.revoke_device(devices, OWNER, missing) is False
    assert await device_service.rename_device(devices, OWNER, missing, "x") is False
    assert await device_service.delete_device(devices, OWNER, missing) is False


async def test_non_owner_cannot_rename_or_delete_a_device(maker: async_sessionmaker) -> None:
    devices = SqlDeviceRepo(maker)
    provisioned = await device_service.provision_device(devices, OWNER, "phone")
    # A non-owner capability session can't even see the device (subjects_access
    # USING), so rename/delete are no-ops and the device survives intact.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("location",))
    did = provisioned.device.id
    assert await device_service.rename_device(devices, token, did, "hacked") is False
    assert await device_service.delete_device(devices, token, did) is False
    survivor = next(d for d in await devices.list(OWNER) if d.id == provisioned.device.id)
    assert survivor.label == "phone" and survivor.revoked is False


async def test_non_owner_cannot_provision_a_device(maker: async_sessionmaker) -> None:
    devices = SqlDeviceRepo(maker)
    # A non-owner capability session: the subjects/principals WITH CHECK is_owner
    # policies reject the inserts at the database layer.
    token = SessionContext(principal_kind="capability_token", domain_scopes=("location",))
    with pytest.raises(ProgrammingError):
        await devices.provision(token, label="sneaky", key_hash=keys.hash_key("jb1-X"))
