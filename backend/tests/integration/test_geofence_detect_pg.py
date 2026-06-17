"""Inline geofence detection against real Postgres + PostGIS (Phase 7 Wave 3b).

Walks a device into and out of a circular fence and asserts the hysteresis fires
exactly one enter and one exit `location.geofence_transition` event, and that the
per-(subject, fence) state lands outside.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.locations.geofence import detect_transitions
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_HOME = (40.0, -74.0)  # fence center
_AWAY = (41.0, -74.0)  # ~111 km north — well beyond radius + buffer


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


async def _fence(maker: async_sessionmaker, radius_m: int = 120) -> None:
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', 'Home', 'location') RETURNING id"
                )
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, domain_code, name, center, radius_m)"
                " VALUES (:e, 'location', 'Home',"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r)"
            ),
            {"e": eid, "lat": _HOME[0], "lon": _HOME[1], "r": radius_m},
        )


async def _detect(maker, pid: str, sid: str, point: tuple[float, float], minute: int) -> list[dict]:
    return await detect_transitions(
        maker,
        principal_id=pid,
        subject_id=sid,
        captured_at=datetime(2026, 6, 4, 12, minute, tzinfo=UTC),
        latitude=point[0],
        longitude=point[1],
    )


async def test_walk_in_and_out_emits_one_enter_and_one_exit(maker: async_sessionmaker) -> None:
    pid, sid = await _device(maker)
    await _fence(maker)

    assert await _detect(maker, pid, sid, _HOME, 0) == []  # first inside: debounced
    enter = await _detect(maker, pid, sid, _HOME, 1)
    assert [t["transition"] for t in enter] == ["enter"]

    assert await _detect(maker, pid, sid, _AWAY, 2) == []  # first outside: debounced
    exit_ = await _detect(maker, pid, sid, _AWAY, 3)
    assert [t["transition"] for t in exit_] == ["exit"]

    # Exactly the two transition events landed, typed and stamped to location.
    async with scoped_session(maker, OWNER) as session:
        events = (
            (
                await session.execute(
                    text(
                        "SELECT payload->>'transition' FROM app.events"
                        " WHERE type = 'location.geofence_transition' AND domain_code = 'location'"
                        " ORDER BY occurred_at"
                    )
                )
            )
            .scalars()
            .all()
        )
        state = (
            await session.execute(
                text("SELECT state FROM app.geofence_state WHERE subject_id = :s"), {"s": sid}
            )
        ).scalar()
    assert list(events) == ["enter", "exit"]
    assert state == "outside"


async def test_low_accuracy_fix_is_ignored(maker: async_sessionmaker) -> None:
    pid, sid = await _device(maker)
    await _fence(maker)
    # A 500 m-accuracy fix at the center must not advance any geofence state.
    out = await detect_transitions(
        maker,
        principal_id=pid,
        subject_id=sid,
        captured_at=datetime(2026, 6, 4, 12, 0, tzinfo=UTC),
        latitude=_HOME[0],
        longitude=_HOME[1],
        accuracy_m=500.0,
    )
    assert out == []
    async with scoped_session(maker, OWNER) as session:
        rows = (
            await session.execute(
                text("SELECT count(*) FROM app.geofence_state WHERE subject_id = :s"), {"s": sid}
            )
        ).scalar()
    assert rows == 0
