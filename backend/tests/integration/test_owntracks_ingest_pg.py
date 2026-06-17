"""OwnTracks ingest against real Postgres (Phase 7 Wave 3a).

Proves a device writes a fix under its subject-pinned session, that the natural
key makes OwnTracks retries idempotent, and that the owner can read what landed.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.locations import LocationFix, SqlLocationRepo
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


async def _device(maker: async_sessionmaker) -> tuple[str, str]:
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


def _fix(when: datetime) -> LocationFix:
    return LocationFix(
        captured_at=when, latitude=40.0, longitude=-74.0, accuracy_m=5.0, velocity_mps=10.0
    )


async def test_ingest_stores_a_fix_and_is_idempotent(maker: async_sessionmaker) -> None:
    repo = SqlLocationRepo(maker)
    pid, sid = await _device(maker)
    when = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    assert await repo.ingest_fix(principal_id=pid, subject_id=sid, fix=_fix(when)) is True
    # OwnTracks resends the same fix until it gets a 200 — the retry is a no-op.
    assert await repo.ingest_fix(principal_id=pid, subject_id=sid, fix=_fix(when)) is False

    async with scoped_session(maker, OWNER) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT velocity_mps, ST_Y(geog::geometry) AS lat, ST_X(geog::geometry) AS lon"
                    " FROM app.location_fixes WHERE subject_id = :s"
                ),
                {"s": sid},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].velocity_mps == 10.0
    # The generated geography mirrors the stored lat/lon (X=lon, Y=lat).
    assert (rows[0].lat, rows[0].lon) == (40.0, -74.0)
