"""Phone vs panel against real Postgres (SECURITY_REVIEW_FIXES_PLAN S3.2).

Phones and panels are both `device_key` principals; what tells them apart is the subject's
`device_role` (migration 0211). The SQL filter, under the narrow `login` context the auth repo
runs in, is what these pin: a phone key is no panel's, a panel key is no phone's, MQTT takes
either, and the startup count finds a role-less unit that has reported as a panel.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys, service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.devices import service as device_service
from jbrain.devices.repo import SqlDeviceRepo, count_unroled_reporting_devices
from jbrain.main import create_app
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


async def test_the_role_decides_which_door_a_device_key_opens(maker: async_sessionmaker) -> None:
    auth = SqlAuthRepo(maker)
    devices = SqlDeviceRepo(maker)
    phone = await device_service.provision_device(devices, OWNER, "S3.2 phone")
    jpet = await device_service.provision_or_reflash(
        devices, OWNER, "panel S3two", device_role="jpet"
    )
    display = await device_service.provision_or_reflash(
        devices, OWNER, "panel S3desk", device_role="display"
    )

    assert await service.authenticate_device(auth, phone.key, "phone") is not None
    assert await service.authenticate_device(auth, phone.key, "panel") is None
    for panel in (jpet, display):
        assert await service.authenticate_device(auth, panel.key, "panel") is not None
        assert await service.authenticate_device(auth, panel.key, "phone") is None
    # MQTT asks for either class and keeps working for both.
    for any_key in (phone.key, jpet.key, display.key):
        assert await service.authenticate_device(auth, any_key) is not None

    assert await service.mint_dashboard_session(auth, phone.key) is not None
    assert await service.mint_dashboard_session(auth, jpet.key) is None


async def test_a_subjectless_device_key_stays_a_phone(maker: async_sessionmaker) -> None:
    """A key with no subject has no role: it keeps the phone paths it always had, and no
    panel route takes it."""
    auth = SqlAuthRepo(maker)
    key = keys.generate_owner_key()
    await auth.create_principal("device_key", keys.hash_key(key), "legacy phone")
    assert await service.authenticate_device(auth, key, "phone") is not None
    assert await service.authenticate_device(auth, key, "panel") is None


async def test_the_http_doors(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker,
) -> None:
    devices = SqlDeviceRepo(maker)
    phone = await device_service.provision_device(devices, OWNER, "S3.2 http phone")
    panel = await device_service.provision_or_reflash(
        devices, OWNER, "panel S3http", device_role="jpet"
    )
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:

        def bearer(key: str) -> dict[str, str]:
            return {"Authorization": f"Bearer {key}"}

        assert client.get("/api/endpoint/settings", headers=bearer(panel.key)).status_code == 200
        assert client.get("/api/endpoint/settings", headers=bearer(phone.key)).status_code == 401
        mint = client.post("/api/session/mint", json={"device_key": panel.key})
        assert mint.status_code == 401
        client.cookies.clear()
        mint = client.post("/api/session/mint", json={"device_key": phone.key})
        assert mint.status_code == 204
        client.cookies.clear()
        loc = {"_type": "location", "lat": 40.0, "lon": -74.0, "tst": 1_800_000_000}
        assert client.post("/api/owntracks", json=loc, auth=("panel", panel.key)).status_code == 401


async def test_the_startup_count_finds_a_roleless_unit_that_reported(
    maker: async_sessionmaker,
) -> None:
    devices = SqlDeviceRepo(maker)
    before = await count_unroled_reporting_devices(maker, OWNER)
    stray = await device_service.provision_device(devices, OWNER, "panel stray legacy")
    roled = await device_service.provision_or_reflash(
        devices, OWNER, "panel S3counted", device_role="display"
    )
    auth = SqlAuthRepo(maker)
    async with scoped_session(maker, OWNER) as s:
        for key in (stray.key, roled.key):
            principal = await service.authenticate_device(auth, key)
            assert principal is not None
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_status (principal_id) VALUES (CAST(:p AS uuid))"
                    " ON CONFLICT DO NOTHING"
                ),
                {"p": principal.id},
            )
    # Only the role-less one counts; the role'd panel is what a panel should be.
    assert await count_unroled_reporting_devices(maker, OWNER) == before + 1
