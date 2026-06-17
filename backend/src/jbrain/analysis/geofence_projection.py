"""Project a Place entity's `geofence` predicate into `app.place_geofence`.

The mirror is NOT a source of truth — it is a denormalized, PostGIS-queryable
view of the note-sourced `geofence` fact on a Place entity (notes are the sole
sources of truth, #7). After a note's facts settle (or a note is purged) this
re-derives every touched Place's CURRENT geofence from its active `geofence`
fact and upserts one row — or deletes the row when no live geofence remains. It
runs inside the caller's transaction on the owner-scoped session (the pipeline's
SYSTEM_CTX, or the owner deleting a note), so it is atomic with the graph write
and idempotent on re-analysis. Geometry is migration-owned, so writes go through
raw `ST_*` SQL; the ORM model carries only the scalar columns.
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.models.analysis import Entity, Fact
from jbrain.models.location import PlaceGeofence

_GEOFENCE = "geofence"
_PLACE = "place"


def _geometry(
    value_json: Any,
) -> tuple[float | None, float | None, float | None, str | None] | None:
    """A `geofence` fact's value as (center_lat, center_lon, radius_m, polygon_wkt),
    or None when it carries no usable geometry. A polygon wins over a circle; the
    place_geofence CHECK requires exactly one of the two, so the unused half is None.

    Shape (schema `geofence`): {center:{latitude,longitude}, radiusMeters, polygon}."""
    if not isinstance(value_json, dict):
        return None
    polygon = value_json.get("polygon")
    if isinstance(polygon, str) and polygon.strip():
        return (None, None, None, polygon.strip())
    center = value_json.get("center")
    radius = value_json.get("radiusMeters")
    if isinstance(center, dict) and isinstance(radius, (int, float)):
        lat, lon = center.get("latitude"), center.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return (float(lat), float(lon), float(radius), None)
    return None


async def project_place_geofences(session: AsyncSession, entity_ids: set[uuid.UUID]) -> None:
    """Re-derive and upsert (or remove) the geofence mirror for each Place entity.
    Non-place / already-deleted / fence-less entities are no-ops or deletions, so
    it is safe to pass the whole touched/purged set."""
    if not entity_ids:
        return
    ents = (
        await session.execute(
            select(Entity).where(Entity.id.in_(entity_ids), func.lower(Entity.kind) == _PLACE)
        )
    ).scalars()
    for ent in ents:
        await _project_one(session, ent)


async def _project_one(session: AsyncSession, ent: Entity) -> None:
    fact = (
        await session.execute(
            select(Fact)
            .where(
                Fact.entity_id == ent.id,
                Fact.predicate == _GEOFENCE,
                Fact.status == "active",
                Fact.valid_to.is_(None),  # the current geofence only
            )
            .order_by(Fact.valid_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # One row per place: clear and re-derive (no unique constraint to upsert on,
    # and a place has at most one live geofence).
    await session.execute(delete(PlaceGeofence).where(PlaceGeofence.place_entity_id == ent.id))
    if fact is None:
        return
    geom = _geometry(fact.value_json)
    if geom is None:
        return
    lat, lon, radius, polygon = geom
    base = {"eid": str(ent.id), "dom": ent.domain_code, "name": ent.canonical_name}
    if polygon is not None:
        await session.execute(
            text(
                "INSERT INTO app.place_geofence (place_entity_id, domain_code, name, polygon)"
                " VALUES (:eid, :dom, :name, ST_GeogFromText(:wkt))"
            ),
            {**base, "wkt": polygon},
        )
    else:
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, domain_code, name, center, radius_m)"
                " VALUES (:eid, :dom, :name,"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius)"
            ),
            {**base, "lat": lat, "lon": lon, "radius": radius},
        )
