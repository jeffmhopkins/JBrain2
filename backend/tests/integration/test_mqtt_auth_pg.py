"""MQTT device auth against real Postgres (JBrain360 M0).

Proves the go-auth HTTP backend's `/internal/mqtt-auth` path resolves an MQTT
password (the device key) to its principal through the SHIPPED, RLS-scoped
`device_key` lookup — the same `service.authenticate_device` the HTTP ingest
uses — and is fail-closed for an unknown / revoked / wrong-kind key.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _unique_key() -> str:
    # `principals.key_hash` is UNIQUE and the module-scoped container persists rows
    # across tests, so every provisioned key must be distinct or the second insert
    # collides on the unique constraint.
    return f"jb1-{uuid.uuid4().hex}"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _provision(maker: async_sessionmaker, *, kind: str, key: str) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, 'phone', 'device')"
            ),
            {"s": sid},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, :k, :s, :kh)"
            ),
            {"p": pid, "k": kind, "s": sid, "kh": keys.hash_key(key)},
        )
    return pid, sid


async def test_valid_device_key_resolves_to_its_subject(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    device_key = _unique_key()
    pid, sid = await _provision(maker, kind="device_key", key=device_key)

    principal = await auth_service.authenticate_device(repo, device_key)
    assert principal is not None
    assert principal.id == pid
    assert principal.kind == "device_key"
    assert principal.subject_id == sid


async def test_unknown_revoked_and_owner_keys_are_fail_closed(maker: async_sessionmaker) -> None:
    repo = SqlAuthRepo(maker)
    device_key, owner_key = _unique_key(), _unique_key()
    pid, _ = await _provision(maker, kind="device_key", key=device_key)
    await _provision(maker, kind="owner", key=owner_key)

    # Unknown key.
    assert await auth_service.authenticate_device(repo, _unique_key()) is None
    # An owner key presented on the device path is kind-filtered out (L4).
    assert await auth_service.authenticate_device(repo, owner_key) is None

    # Revoked device key no longer authenticates.
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.principals SET revoked_at = now() WHERE id = :p"), {"p": pid}
        )
    assert await auth_service.authenticate_device(repo, device_key) is None
