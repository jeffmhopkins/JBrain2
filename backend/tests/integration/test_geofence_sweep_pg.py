"""The geofence_sweep reconciler against real Postgres + PostGIS (Phase 7 Wave 3c).

The sweep is the full-owner backstop for the two best-effort inline paths:

  * it rebuilds the `place_geofence` spatial mirror from the graph (so a dropped
    projector hook cannot leave the mirror missing or stale), and
  * it re-evaluates each device subject's latest fix (so a dropped inline
    transition still self-heals), idempotently — a settled stream emits nothing.

It runs as the full owner, so it reaches every subject's pinned track and writes
the mirror only a full owner may write.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.locations.geofence import sweep_geofences
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_HOME = (40.0, -74.0)  # fence center, and where the device sits


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


async def _place_with_geofence_fact(maker: async_sessionmaker, radius: int = 150) -> str:
    """A Place entity + an active note-sourced `geofence` fact, but NO mirror row —
    the state after a dropped projector hook. Returns the entity id."""
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER,
        client_id=f"geo-{uuid.uuid4().hex[:8]}",
        domain="location",
        destination=None,
        body="Home fence.",
    )
    value = json.dumps(
        {"center": {"latitude": _HOME[0], "longitude": _HOME[1]}, "radiusMeters": radius}
    )
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
                "INSERT INTO app.facts"
                " (id, entity_id, predicate, kind, statement, value_json, assertion,"
                "  reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'geofence', 'state', 'Home fence',"
                "  cast(:v AS jsonb), 'asserted', :now, :n, 'test', 'v1', 'location')"
            ),
            {"e": eid, "v": value, "now": datetime.now(UTC), "n": note.id},
        )
    return str(eid)


async def _fix(maker: async_sessionmaker, pid: str, sid: str, minute: int) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :ts, :lat, :lon)"
            ),
            {
                "s": sid,
                "p": pid,
                "ts": datetime(2026, 6, 4, 12, minute, tzinfo=UTC),
                "lat": _HOME[0],
                "lon": _HOME[1],
            },
        )


async def test_sweep_rebuilds_a_missing_mirror_then_removes_a_stale_one(
    maker: async_sessionmaker,
) -> None:
    eid = await _place_with_geofence_fact(maker)
    count = text("SELECT count(*) FROM app.place_geofence WHERE place_entity_id = :e")

    # The projector hook was dropped, so no mirror exists yet; the sweep rebuilds it.
    async with scoped_session(maker, OWNER) as session:
        assert (await session.execute(count, {"e": eid})).scalar() == 0
    await sweep_geofences(maker)
    async with scoped_session(maker, OWNER) as session:
        row = (
            await session.execute(
                text(
                    "SELECT radius_m, ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lon"
                    " FROM app.place_geofence WHERE place_entity_id = :e"
                ),
                {"e": eid},
            )
        ).one()
    assert (row.radius_m, row.lat, row.lon) == (150.0, 40.0, -74.0)

    # Now the fact is superseded but its supersession hook was dropped — the sweep
    # removes the now-unbacked mirror row.
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.facts SET status = 'superseded' WHERE entity_id = :e"), {"e": eid}
        )
    await sweep_geofences(maker)
    async with scoped_session(maker, OWNER) as session:
        assert (await session.execute(count, {"e": eid})).scalar() == 0


async def test_sweep_heals_a_missed_enter_then_is_idempotent(maker: async_sessionmaker) -> None:
    await _place_with_geofence_fact(maker)
    pid, sid = await _device(maker)
    await _fix(maker, pid, sid, minute=0)  # the device's latest fix is inside the fence

    # First sweep projects the fence and counts one confirming fix (debounce); the
    # second crosses the CONFIRM_FIXES threshold and heals the missed enter.
    assert await sweep_geofences(maker) == 0
    assert await sweep_geofences(maker) == 1
    # Idempotent: with the state settled inside, re-running emits nothing more (E4).
    assert await sweep_geofences(maker) == 0

    async with scoped_session(maker, OWNER) as session:
        events = (
            (
                await session.execute(
                    text(
                        "SELECT payload->>'transition' FROM app.events"
                        " WHERE type = 'location.geofence_transition'"
                        " AND payload->>'subject_id' = :s"
                    ),
                    {"s": sid},
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
    assert list(events) == ["enter"]
    assert state == "inside"
