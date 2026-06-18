"""The place-geofence projector against real Postgres + PostGIS (Phase 7 Wave 3b).

A note-sourced `geofence` fact on a Place entity projects into one
`app.place_geofence` row; superseding the fact removes the mirror row.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.analysis.geofence_projection import project_place_geofences
from jbrain.db.session import scoped_session
from jbrain.notes.repo import SqlNotesRepo
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


async def _place_with_geofence_fact(maker: async_sessionmaker) -> tuple[str, str]:
    """Create a Place entity + an active `geofence` fact (sourced to a real note).
    Returns (entity_id, fact_id)."""
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER,
        client_id=f"geo-{uuid.uuid4().hex[:8]}",
        domain="location",
        destination=None,
        body="Home is a 150 m fence around the house.",
    )
    value = json.dumps({"center": {"latitude": 40.0, "longitude": -74.0}, "radiusMeters": 150})
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', 'Home', 'location') RETURNING id"
                )
            )
        ).scalar()
        fid = (
            await session.execute(
                text(
                    "INSERT INTO app.facts"
                    " (id, entity_id, predicate, kind, statement, value_json, assertion,"
                    "  reported_at, note_id, extractor, prompt_version, domain_code)"
                    " VALUES (gen_random_uuid(), :e, 'geofence', 'state', 'Home fence',"
                    "  cast(:v AS jsonb), 'asserted', :now, :n, 'test', 'v1', 'location')"
                    " RETURNING id"
                ),
                {"e": eid, "v": value, "now": datetime.now(UTC), "n": note.id},
            )
        ).scalar()
    return str(eid), str(fid)


async def test_projects_a_geofence_fact_then_supersession_removes_the_row(
    maker: async_sessionmaker,
) -> None:
    eid, fid = await _place_with_geofence_fact(maker)
    count = text("SELECT count(*) FROM app.place_geofence WHERE place_entity_id = :e")

    async with scoped_session(maker, OWNER) as session:
        await project_place_geofences(session, {uuid.UUID(eid)})
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

    # Superseding the fact and re-projecting drops the mirror row.
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.facts SET status = 'superseded' WHERE id = :f"), {"f": fid}
        )
        await project_place_geofences(session, {uuid.UUID(eid)})
        assert (await session.execute(count, {"e": eid})).scalar() == 0
