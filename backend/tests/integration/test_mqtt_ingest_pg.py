"""MQTT ingest consumer against real Postgres (JBrain360 M1).

Proves a published OwnTracks location lands in the shipped `location_fixes`
hypertable under the topic owner's subject (resolved via the device principal,
never the payload), idempotently — and that a non-location body or an unknown
publisher is dropped.
"""

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from jbrain.locations import SqlLocationRepo
from jbrain.mqtt.consumer import handle_message
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _loc(tst: int) -> bytes:
    return json.dumps({"_type": "location", "lat": 40.0, "lon": -74.0, "tst": tst}).encode()


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _provision(maker: async_sessionmaker) -> tuple[str, str]:
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
                " VALUES (:p, 'device_key', :s, :kh)"
            ),
            {"p": pid, "s": sid, "kh": uuid.uuid4().hex},
        )
    return pid, sid


async def _count(maker: async_sessionmaker, sid: str) -> int:
    async with scoped_session(maker, OWNER) as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM app.location_fixes WHERE subject_id = cast(:s AS uuid)"),
                {"s": sid},
            )
        ).scalar_one()


async def test_published_location_lands_under_the_subject_and_is_idempotent(
    maker: async_sessionmaker,
) -> None:
    auth, sink = SqlAuthRepo(maker), SqlLocationRepo(maker)
    pid, sid = await _provision(maker)
    topic = f"owntracks/{pid}/phone"

    assert await handle_message(auth, sink, maker, topic=topic, payload=_loc(1700000000)) is True
    # A redelivery of the same fix is a no-op (idempotent on the natural key).
    assert await handle_message(auth, sink, maker, topic=topic, payload=_loc(1700000000)) is False
    assert await _count(maker, sid) == 1


async def test_non_location_and_unknown_publisher_are_dropped(maker: async_sessionmaker) -> None:
    auth, sink = SqlAuthRepo(maker), SqlLocationRepo(maker)
    pid, sid = await _provision(maker)

    # A transition body on the device's base topic: acknowledged, not stored.
    assert (
        await handle_message(
            auth, sink, maker, topic=f"owntracks/{pid}/phone", payload=b'{"_type":"transition"}'
        )
        is False
    )
    # A location on an unknown principal's topic: dropped (no row).
    assert (
        await handle_message(
            auth, sink, maker, topic=f"owntracks/{uuid.uuid4()}/phone", payload=_loc(1700000001)
        )
        is False
    )
    assert await _count(maker, sid) == 0
