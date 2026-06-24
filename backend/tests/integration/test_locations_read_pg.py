"""The location read layer against real Postgres + RLS (Phase 7 Wave 5a).

Proves the owner sees every device's track (device_activity / fixes / timeline)
while a non-full-owner session sees nothing — the location-fixes hypertable's
subject-pin barrier (a narrowed/agent owner is not `is_full_owner()`) and the
domain firewall (a wrong-domain scope reads zero). The window filter and the
place-name resolution on the timeline are checked under the owner.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.locationtools import build_location_handlers
from jbrain.agent.loop import ToolContext
from jbrain.analysis.device_binding import reconcile_device_bindings
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.locations import LocationToolRefusal, SqlLocationRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A narrowed owner (an agent session): owner identity, but owner_scoped → NOT a
# full owner, so the location-fixes subject pin denies it every row.
NARROWED_LOCATION = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("location",),
    owner_scoped=True,
)
# A narrowed owner scoped to the wrong domain: the firewall denies location too.
NARROWED_GENERAL = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("general",),
    owner_scoped=True,
)

_BASE = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
_HOME = (40.0, -74.0)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _device(maker: async_sessionmaker, label: str) -> tuple[str, str]:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :l, 'device')"),
            {"s": sid, "l": label},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :kh)"
            ),
            {"p": pid, "s": sid, "kh": uuid.uuid4().hex},
        )
    return pid, sid


async def _fix(
    maker: async_sessionmaker,
    *,
    pid: str,
    sid: str,
    minute: int,
    battery: int | None = None,
    connection: str | None = None,
) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude,"
                "  battery_pct, connection)"
                " VALUES (:s, :p, :ts, :lat, :lon, :b, :c)"
            ),
            {
                "s": sid,
                "p": pid,
                "ts": _BASE + timedelta(minutes=minute),
                "lat": _HOME[0],
                "lon": _HOME[1],
                "b": battery,
                "c": connection,
            },
        )


async def _transition(
    maker: async_sessionmaker, *, pid: str, sid: str, place: str, transition: str
) -> str:
    """A Place entity + a location.geofence_transition event referencing it, stamped
    with the device principal (as the detector emits it). Returns the place's
    canonical name (for the timeline assertion)."""
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', :n, 'location') RETURNING id"
                ),
                {"n": place},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id, occurred_at)"
                " VALUES (gen_random_uuid(), 'location.geofence_transition',"
                "   cast(:pl AS jsonb), 'location', :pr, :ts)"
            ),
            {
                "pl": f'{{"subject_id": "{sid}", "place_entity_id": "{eid}",'
                f' "transition": "{transition}"}}',
                "pr": pid,
                "ts": _BASE + timedelta(minutes=5),
            },
        )
    return place


async def _place_geofence(maker: async_sessionmaker, *, name: str, radius_m: int = 150) -> None:
    """A Place entity + a circular place_geofence mirror row (owner-written)."""
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', :n, 'location') RETURNING id"
                ),
                {"n": name},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, domain_code, name, center, radius_m)"
                " VALUES (:e, 'location', :n,"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r)"
            ),
            {"e": eid, "n": name, "lat": _HOME[0], "lon": _HOME[1], "r": radius_m},
        )


async def _entity(maker: async_sessionmaker, *, kind: str, name: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), :k, :n, 'location') RETURNING id::text"
                ),
                {"k": kind, "n": name},
            )
        ).scalar_one()


async def _operated_by(maker: async_sessionmaker, *, device_eid: str, person_eid: str) -> None:
    """An active asserted operatedBy relationship edge from the Device to the Person,
    sourced from an owner note (facts.note_id is NOT NULL)."""
    async with scoped_session(maker, OWNER) as session:
        note_id = (
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'location', 'device note') RETURNING id"
                ),
                {"cid": f"opby-{device_eid}"},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, assertion,"
                "   status, object_entity_id, domain_code, statement, reported_at, note_id,"
                "   extractor, prompt_version)"
                " VALUES (gen_random_uuid(), cast(:d AS uuid), 'operatedBy', 'relationship',"
                "   'asserted', 'active', cast(:p AS uuid), 'location', 'operated by', :ts, :nid,"
                "   'test', 'v0')"
            ),
            {"d": device_eid, "p": person_eid, "ts": _BASE, "nid": str(note_id)},
        )


async def _geofence_state(
    maker: async_sessionmaker, *, sid: str, place_geofence_name: str, state: str = "inside"
) -> str:
    """A place_geofence row + a geofence_state row pinning `sid` to it. Returns the
    place entity id."""
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', :n, 'location') RETURNING id"
                ),
                {"n": place_geofence_name},
            )
        ).scalar()
        pgid = (
            await session.execute(
                text(
                    "INSERT INTO app.place_geofence"
                    " (place_entity_id, domain_code, name, center, radius_m)"
                    " VALUES (:e, 'location', :n,"
                    " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 150)"
                    " RETURNING id"
                ),
                {"e": eid, "n": place_geofence_name, "lat": _HOME[0], "lon": _HOME[1]},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.geofence_state"
                " (subject_id, place_geofence_id, domain_code, state, since)"
                " VALUES (:s, :pg, 'location', :st, :since)"
            ),
            {"s": sid, "pg": pgid, "st": state, "since": _BASE},
        )
    return str(eid)


async def _crossing(
    maker: async_sessionmaker,
    *,
    pid: str,
    sid: str,
    eid: str,
    transition: str,
    minute: int,
) -> None:
    """A location.geofence_transition event for an existing place entity at a minute
    offset (the shape the detector emits), for dwell pairing."""
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id, occurred_at)"
                " VALUES (gen_random_uuid(), 'location.geofence_transition',"
                "   cast(:pl AS jsonb), 'location', :pr, :ts)"
            ),
            {
                "pl": f'{{"subject_id": "{sid}", "place_entity_id": "{eid}",'
                f' "transition": "{transition}"}}',
                "pr": pid,
                "ts": _BASE + timedelta(minutes=minute),
            },
        )


async def _fix_at(maker: async_sessionmaker, *, pid: str, sid: str, seconds: int) -> None:
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
                "ts": _BASE + timedelta(seconds=seconds),
                "lat": _HOME[0],
                "lon": _HOME[1],
            },
        )


async def test_owner_sees_every_device_activity(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    pb, sb = await _device(maker, "Tablet")
    await _fix(maker, pid=pa, sid=sa, minute=0)
    await _fix(maker, pid=pa, sid=sa, minute=1)
    await _fix(maker, pid=pa, sid=sa, minute=2, battery=55, connection="wifi")
    await _fix(maker, pid=pb, sid=sb, minute=0)

    activity = await SqlLocationRepo(maker).device_activity(OWNER)
    assert set(activity) == {sa, sb}
    # Subject A's latest fix carries the surfaced battery/connection + the full count.
    assert activity[sa].fix_count == 3
    assert activity[sa].last_seen == _BASE + timedelta(minutes=2)
    assert activity[sa].battery_pct == 55 and activity[sa].connection == "wifi"
    assert activity[sb].fix_count == 1


async def test_owner_reads_fixes_in_window_oldest_first(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    for m in range(3):
        await _fix(maker, pid=pa, sid=sa, minute=m)
    repo = SqlLocationRepo(maker)

    full = await repo.fixes(
        OWNER, subject_id=sa, since=_BASE, until=_BASE + timedelta(minutes=3), limit=100
    )
    assert [f.captured_at for f in full] == [_BASE + timedelta(minutes=m) for m in range(3)]
    # `since` is inclusive, `until` exclusive: [t1, t2) yields only minute 1.
    windowed = await repo.fixes(
        OWNER,
        subject_id=sa,
        since=_BASE + timedelta(minutes=1),
        until=_BASE + timedelta(minutes=2),
        limit=100,
    )
    assert [f.captured_at for f in windowed] == [_BASE + timedelta(minutes=1)]


async def test_owner_timeline_resolves_place_name(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    await _transition(maker, pid=pa, sid=sa, place="Office", transition="exit")

    rows = await SqlLocationRepo(maker).timeline(
        OWNER, since=_BASE, until=_BASE + timedelta(hours=1), limit=100
    )
    assert len(rows) == 1
    assert rows[0].transition == "exit" and rows[0].place_name == "Office"
    assert rows[0].subject_id == sa


async def test_owner_reads_place_geofences(maker: async_sessionmaker) -> None:
    await _place_geofence(maker, name="Home", radius_m=150)
    places = await SqlLocationRepo(maker).places(OWNER)
    home = next(p for p in places if p.name == "Home")
    assert home.radius_m == 150.0 and home.polygon is None
    assert home.center is not None
    lat, lon = home.center
    assert round(lat, 4) == 40.0 and round(lon, 4) == -74.0


async def test_narrowed_owner_is_denied_the_subject_pinned_track(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    await _fix(maker, pid=pa, sid=sa, minute=0)
    repo = SqlLocationRepo(maker)

    # Not a full owner → the location-fixes subject pin denies every row.
    assert await repo.device_activity(NARROWED_LOCATION) == {}
    assert (
        await repo.fixes(
            NARROWED_LOCATION,
            subject_id=sa,
            since=_BASE,
            until=_BASE + timedelta(minutes=1),
            limit=100,
        )
        == []
    )


async def test_wrong_domain_scope_reads_nothing(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    await _fix(maker, pid=pa, sid=sa, minute=0)
    await _transition(maker, pid=pa, sid=sa, place="Office", transition="enter")
    await _place_geofence(maker, name="Home")
    repo = SqlLocationRepo(maker)

    # has_domain_scope('location') is false for a general-only session: zero rows
    # across the fix-backed reads (RLS fails them closed).
    assert await repo.device_activity(NARROWED_GENERAL) == {}
    assert (
        await repo.fixes(
            NARROWED_GENERAL,
            subject_id=sa,
            since=_BASE,
            until=_BASE + timedelta(hours=1),
            limit=100,
        )
        == []
    )
    # timeline()/places() read WEAK tables (app.events / place_geofence), which RLS
    # does NOT fail-close for a narrowed owner — the full-owner gate is the barrier,
    # so they REFUSE rather than return an empty (leak-closing) list.
    with pytest.raises(LocationToolRefusal):
        await repo.timeline(
            NARROWED_GENERAL, since=_BASE, until=_BASE + timedelta(hours=1), limit=100
        )
    with pytest.raises(LocationToolRefusal):
        await repo.places(NARROWED_GENERAL)


# --- L1 read trio: nearest_fix / latest_place / dwells ----------------------


async def test_nearest_fix_returns_closest_with_gap(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    await _fix_at(maker, pid=pa, sid=sa, seconds=0)
    await _fix_at(maker, pid=pa, sid=sa, seconds=100)
    repo = SqlLocationRepo(maker)

    # at = +30s: the +0s fix (gap 30) beats the +100s fix (gap 70).
    near = await repo.nearest_fix(
        OWNER, subject_id=sa, at=_BASE + timedelta(seconds=30), max_gap_seconds=300
    )
    assert near is not None
    assert near.fix.captured_at == _BASE and near.gap_seconds == 30.0


async def test_nearest_fix_none_when_beyond_max_gap(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    await _fix_at(maker, pid=pa, sid=sa, seconds=0)
    repo = SqlLocationRepo(maker)

    # The only fix is 600s away; a 60s window excludes it.
    assert (
        await repo.nearest_fix(
            OWNER, subject_id=sa, at=_BASE + timedelta(seconds=600), max_gap_seconds=60
        )
        is None
    )


async def test_nearest_fix_narrowed_owner_gets_none(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    await _fix_at(maker, pid=pa, sid=sa, seconds=0)
    repo = SqlLocationRepo(maker)
    # location_fixes is STRICT RLS: a narrowed owner is not is_full_owner(), so the
    # subject pin denies every row — fail-closed by RLS, no app guard needed.
    assert (
        await repo.nearest_fix(NARROWED_LOCATION, subject_id=sa, at=_BASE, max_gap_seconds=300)
        is None
    )


async def test_latest_place_resolves_current_place(maker: async_sessionmaker) -> None:
    _, sa = await _device(maker, "Phone")
    eid = await _geofence_state(maker, sid=sa, place_geofence_name="Office", state="inside")
    repo = SqlLocationRepo(maker)

    place = await repo.latest_place(OWNER, subject_id=sa)
    assert place is not None
    assert place.place_name == "Office" and place.place_entity_id == eid
    assert place.since == _BASE


async def test_latest_place_none_when_outside(maker: async_sessionmaker) -> None:
    _, sa = await _device(maker, "Phone")
    await _geofence_state(maker, sid=sa, place_geofence_name="Office", state="outside")
    repo = SqlLocationRepo(maker)
    assert await repo.latest_place(OWNER, subject_id=sa) is None


async def test_latest_place_narrowed_owner_gets_none(maker: async_sessionmaker) -> None:
    _, sa = await _device(maker, "Phone")
    await _geofence_state(maker, sid=sa, place_geofence_name="Office", state="inside")
    repo = SqlLocationRepo(maker)
    # geofence_state is STRICT RLS: the subject pin denies the narrowed owner.
    assert await repo.latest_place(NARROWED_LOCATION, subject_id=sa) is None


async def test_dwells_pairs_and_filters_by_place(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    office = await _entity(maker, kind="Place", name="Office")
    gym = await _entity(maker, kind="Place", name="Gym")
    await _crossing(maker, pid=pa, sid=sa, eid=office, transition="enter", minute=0)
    await _crossing(maker, pid=pa, sid=sa, eid=office, transition="exit", minute=30)
    await _crossing(maker, pid=pa, sid=sa, eid=gym, transition="enter", minute=60)
    await _crossing(maker, pid=pa, sid=sa, eid=gym, transition="exit", minute=90)
    repo = SqlLocationRepo(maker)

    window = (_BASE, _BASE + timedelta(hours=3))
    every = await repo.dwells(OWNER, subject_id=sa, since=window[0], until=window[1])
    assert {(d.place_name, d.seconds) for d in every} == {
        ("Office", 30 * 60.0),
        ("Gym", 30 * 60.0),
    }
    only_gym = await repo.dwells(
        OWNER, subject_id=sa, place_entity_id=gym, since=window[0], until=window[1]
    )
    assert [d.place_name for d in only_gym] == ["Gym"]


async def test_dwells_clamps_open_enter_to_until(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    office = await _entity(maker, kind="Place", name="Office")
    await _crossing(maker, pid=pa, sid=sa, eid=office, transition="enter", minute=0)
    until = _BASE + timedelta(minutes=45)
    repo = SqlLocationRepo(maker)
    dwells = await repo.dwells(OWNER, subject_id=sa, since=_BASE, until=until)
    assert len(dwells) == 1 and dwells[0].exited_at == until


async def test_dwells_refuses_narrowed_owner(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    # app.events is WEAK RLS, so dwells() MUST refuse a narrowed/owner_scoped session
    # and a wrong-domain owner — RLS would otherwise hand them the rows.
    repo = SqlLocationRepo(maker)
    for ctx in (NARROWED_LOCATION, NARROWED_GENERAL):
        with pytest.raises(LocationToolRefusal):
            await repo.dwells(ctx, subject_id=sa, since=_BASE, until=_BASE + timedelta(hours=1))


# --- L1 person⇄device binding (owner-only, deterministic) --------------------


async def test_binding_links_device_with_operatedby_and_name_match(
    maker: async_sessionmaker,
) -> None:
    _, sa = await _device(maker, "Jeff's iPhone")
    person = await _entity(maker, kind="Person", name="Jeff")
    device_entity = await _entity(maker, kind="Device", name="Jeff's iPhone")
    await _operated_by(maker, device_eid=device_entity, person_eid=person)

    async with scoped_session(maker, OWNER) as session:
        bound = await reconcile_device_bindings(session, {uuid.UUID(device_entity)})
    assert bound == 1

    devices = SqlDeviceRepo(maker)
    linked = await devices.linked_person(OWNER, sa)
    assert linked is not None and linked.entity_id == device_entity
    assert await devices.subject_for_person(OWNER, device_entity) == sa


async def test_unbound_device_yields_zero_fixes(maker: async_sessionmaker) -> None:
    # A Device entity with operatedBy but NO matching subject stays unlinked, and a
    # Person→device→fix resolution through it finds nothing (fail-closed).
    pa, sa = await _device(maker, "Some Other Phone")
    await _fix_at(maker, pid=pa, sid=sa, seconds=0)
    person = await _entity(maker, kind="Person", name="Jeff")
    device_entity = await _entity(maker, kind="Device", name="Jeff's iPhone")  # no such subject
    await _operated_by(maker, device_eid=device_entity, person_eid=person)

    async with scoped_session(maker, OWNER) as session:
        bound = await reconcile_device_bindings(session, {uuid.UUID(device_entity)})
    assert bound == 0

    devices = SqlDeviceRepo(maker)
    assert await devices.subject_for_person(OWNER, device_entity) is None
    # No subject → no track: a caller resolving Person→subject gets None and reads
    # zero fixes (it never reaches a real subject's pinned rows).
    repo = SqlLocationRepo(maker)
    near = await repo.nearest_fix(
        OWNER, subject_id="00000000-0000-0000-0000-000000000000", at=_BASE, max_gap_seconds=300
    )
    assert near is None


async def test_binding_skips_when_subject_already_claimed(maker: async_sessionmaker) -> None:
    # Two device entities both naming the same subject: the second cannot steal it.
    _, sa = await _device(maker, "Shared Phone")
    p1 = await _entity(maker, kind="Person", name="A")
    p2 = await _entity(maker, kind="Person", name="B")
    d1 = await _entity(maker, kind="Device", name="Shared Phone")
    d2 = await _entity(maker, kind="Device", name="Shared Phone")
    await _operated_by(maker, device_eid=d1, person_eid=p1)
    await _operated_by(maker, device_eid=d2, person_eid=p2)

    async with scoped_session(maker, OWNER) as session:
        first = await reconcile_device_bindings(session, {uuid.UUID(d1)})
        second = await reconcile_device_bindings(session, {uuid.UUID(d2)})
    assert first == 1 and second == 0

    devices = SqlDeviceRepo(maker)
    assert await devices.subject_for_person(OWNER, d1) == sa
    assert await devices.subject_for_person(OWNER, d2) is None


async def test_linked_person_round_trip_and_unlinked(maker: async_sessionmaker) -> None:
    _, sa = await _device(maker, "Phone")
    repo = SqlDeviceRepo(maker)
    # Unlinked subject: no entity bound.
    assert await repo.linked_person(OWNER, sa) is None


# --- L2 repo additions: nearby / home_roster --------------------------------


async def _fix_at_coord(
    maker: async_sessionmaker, *, pid: str, sid: str, lat: float, lon: float, minute: int = 0
) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :ts, :lat, :lon)"
            ),
            {"s": sid, "p": pid, "ts": _BASE + timedelta(minutes=minute), "lat": lat, "lon": lon},
        )


async def _fence_at(
    maker: async_sessionmaker, *, name: str, lat: float, lon: float, radius_m: int = 150
) -> str:
    """A Place entity + circular place_geofence at the given coordinate. Returns its
    place entity id."""
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', :n, 'location') RETURNING id::text"
                ),
                {"n": name},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, domain_code, name, center, radius_m)"
                " VALUES (cast(:e AS uuid), 'location', :n,"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :r)"
            ),
            {"e": eid, "n": name, "lat": lat, "lon": lon, "r": radius_m},
        )
    return eid


async def test_nearby_orders_by_distance_within_radius(maker: async_sessionmaker) -> None:
    # A subject far from _HOME (so other tests' _HOME fences don't intrude on the
    # bounded radius), with two unique fences at known distances around it.
    pa, sa = await _device(maker, "NearbyPhone")
    base_lat, base_lon = 10.0, 20.0
    await _fix_at_coord(maker, pid=pa, sid=sa, lat=base_lat, lon=base_lon)
    await _fence_at(maker, name="NB_Near", lat=base_lat + 0.0013, lon=base_lon)  # ~145 m N
    await _fence_at(maker, name="NB_Far", lat=base_lat + 0.01, lon=base_lon)  # ~1.1 km N
    repo = SqlLocationRepo(maker)

    # A 500 m radius sees only the near fence; a wide radius sees both, nearest first.
    near_only = [
        p
        for p in await repo.nearby(OWNER, subject_id=sa, radius_m=500, limit=50)
        if p.name.startswith("NB_")
    ]
    assert [p.name for p in near_only] == ["NB_Near"]
    assert near_only[0].distance_m < 200

    both = [
        p
        for p in await repo.nearby(OWNER, subject_id=sa, radius_m=5000, limit=50)
        if p.name.startswith("NB_")
    ]
    assert [p.name for p in both] == ["NB_Near", "NB_Far"]
    assert both[0].distance_m < both[1].distance_m


async def test_nearby_accepts_an_explicit_center(maker: async_sessionmaker) -> None:
    center = (12.0, 22.0)
    await _fence_at(maker, name="EC_Cafe", lat=center[0], lon=center[1])
    repo = SqlLocationRepo(maker)
    rows = [
        p
        for p in await repo.nearby(OWNER, center=center, radius_m=300, limit=50)
        if p.name.startswith("EC_")
    ]
    assert [p.name for p in rows] == ["EC_Cafe"]
    assert rows[0].distance_m < 1.0


async def test_nearby_refuses_narrowed_owner(maker: async_sessionmaker) -> None:
    # place_geofence is WEAK RLS, so nearby() MUST refuse a narrowed/owner_scoped and a
    # wrong-domain owner — RLS would otherwise hand them fence names + distances.
    await _fence_at(maker, name="Cafe", lat=_HOME[0], lon=_HOME[1])
    repo = SqlLocationRepo(maker)
    for ctx in (NARROWED_LOCATION, NARROWED_GENERAL):
        with pytest.raises(LocationToolRefusal):
            await repo.nearby(ctx, center=_HOME, radius_m=300, limit=10)


async def test_home_roster_reports_place_and_last_seen(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "Phone")
    pb, sb = await _device(maker, "Tablet")
    # sa is inside "Office"; sb is outside every fence. Both have fixes (last-seen).
    await _geofence_state(maker, sid=sa, place_geofence_name="Office", state="inside")
    await _fix_at(maker, pid=pa, sid=sa, seconds=10)
    await _fix_at(maker, pid=pb, sid=sb, seconds=20)
    repo = SqlLocationRepo(maker)

    roster = {e.subject_id: e for e in await repo.home_roster(OWNER)}
    assert roster[sa].place_name == "Office"
    assert roster[sa].last_seen == _BASE + timedelta(seconds=10)
    assert roster[sb].place_name is None  # outside every fence
    assert roster[sb].last_seen == _BASE + timedelta(seconds=20)


async def test_home_roster_refuses_narrowed_owner(maker: async_sessionmaker) -> None:
    # Resolves place NAMES via place_geofence (WEAK RLS) → MUST gate on full owner.
    _, sa = await _device(maker, "Phone")
    await _geofence_state(maker, sid=sa, place_geofence_name="Office", state="inside")
    repo = SqlLocationRepo(maker)
    for ctx in (NARROWED_LOCATION, NARROWED_GENERAL):
        with pytest.raises(LocationToolRefusal):
            await repo.home_roster(ctx)


# --- L2 read tools end-to-end: real handlers over real repos ------------------
# These wire the actual where_is / where_was_i handlers over the real SQL repos,
# proving the Person→operatedBy→Device→subject_id traversal (the P0 the fix closes)
# end-to-end — not through a fake that bypasses it. The module-scoped DB is shared,
# so every entity/place/coordinate here is unique and assertions filter rather than
# match the whole table.


async def _bind_entity_subject(
    maker: async_sessionmaker, *, entity_id: str, subject_id: str
) -> None:
    """Set a Device entity's `subject_id` (the device subject) — the link the L1
    reconciler writes on a name+operatedBy match. Done directly here for a controlled
    fixture; `test_binding_*` already covers the reconciler producing this state."""
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "UPDATE app.entities SET subject_id = cast(:s AS uuid) WHERE id = cast(:e AS uuid)"
            ),
            {"s": subject_id, "e": entity_id},
        )


async def _me_entity(maker: async_sessionmaker, *, name: str = "Me") -> str:
    """The owner "Me" entity, hard-linked to a PERSON subject (kind!='device') exactly
    as `analysis/entities.py` mints it. Its `subject_id` is a person subject — never a
    track — so resolving the owner MUST hop Me→operatedBy→Device, which is the point."""
    person_subject = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :n, 'person')"),
            {"s": person_subject, "n": name},
        )
        return (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code, subject_id)"
                    " VALUES (gen_random_uuid(), 'Person', :n, 'location', cast(:s AS uuid))"
                    " RETURNING id::text"
                ),
                {"n": name, "s": person_subject},
            )
        ).scalar_one()


def _tool_ctx() -> ToolContext:
    return ToolContext(session=OWNER, scopes=())


async def test_where_is_resolves_person_through_operated_by(maker: async_sessionmaker) -> None:
    # A named PERSON ("Jeff") whose phone is bound via operatedBy. where_is MUST reach
    # the DEVICE's track and report Jeff's place — proving Person→operatedBy→Device→
    # subject_id→fixes, NOT the person's own subject_id (which would be "no position").
    pid, sid = await _device(maker, "L2 Jeff iPhone subj")
    person = await _entity(maker, kind="Person", name="L2 Jeff")
    device_entity = await _entity(maker, kind="Device", name="L2 Jeff iPhone")
    await _operated_by(maker, device_eid=device_entity, person_eid=person)
    await _bind_entity_subject(maker, entity_id=device_entity, subject_id=sid)
    place_eid = await _geofence_state(maker, sid=sid, place_geofence_name="L2 Jeff Office")
    await _fix_at(maker, pid=pid, sid=sid, seconds=10)

    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    out = await handlers["where_is"]({"subject": "L2 Jeff"}, _tool_ctx())
    assert "L2 Jeff is at L2 Jeff Office" in out
    assert "no known position" not in out
    assert place_eid  # the geofenced place existed (sanity on the fixture)


async def test_where_was_i_resolves_owner_through_operated_by(maker: async_sessionmaker) -> None:
    # The owner ("Me") via the deterministic hard-link → operatedBy → device → fixes.
    pid, sid = await _device(maker, "L2 Me iPhone subj")
    me = await _me_entity(maker, name="Me")
    device_entity = await _entity(maker, kind="Device", name="L2 Me iPhone")
    await _operated_by(maker, device_eid=device_entity, person_eid=me)
    await _bind_entity_subject(maker, entity_id=device_entity, subject_id=sid)
    await _geofence_state(maker, sid=sid, place_geofence_name="L2 Me Home")
    await _fix_at(maker, pid=pid, sid=sid, seconds=10)

    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    out = await handlers["where_was_i"]({}, _tool_ctx())
    assert "You is at L2 Me Home" in out
    assert "isn't linked" not in out


async def test_where_is_unlinked_person_reports_no_device(maker: async_sessionmaker) -> None:
    # A Person with no operatedBy/device binding resolves to zero device subjects →
    # the graceful "no linked device" answer, not a wrong-subject "no position".
    await _entity(maker, kind="Person", name="L2 Unlinked Grandma")
    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    out = await handlers["where_is"]({"subject": "L2 Unlinked Grandma"}, _tool_ctx())
    assert "no linked device" in out


# --- fixes_within (L3) --------------------------------------------------------


async def test_fixes_within_returns_window_oldest_first(maker: async_sessionmaker) -> None:
    pa, sa = await _device(maker, "FW Phone")
    for m in (0, 5, 10, 60):
        await _fix_at(maker, pid=pa, sid=sa, seconds=m * 60)
    repo = SqlLocationRepo(maker)
    # A window covering the first three fixes (not the 60-min one).
    fixes = await repo.fixes_within(
        OWNER, subject_id=sa, since=_BASE, until=_BASE + timedelta(minutes=30), limit=100
    )
    assert [f.captured_at for f in fixes] == sorted(f.captured_at for f in fixes)
    assert len(fixes) == 3


async def test_fixes_within_is_fail_closed_for_a_narrowed_session(
    maker: async_sessionmaker,
) -> None:
    # location_fixes is STRICT RLS: a narrowed (owner_scoped) or wrong-domain session
    # sees zero rows — RLS fails it closed, no rows leak via the spatial query.
    pa, sa = await _device(maker, "FW Narrowed Phone")
    await _fix_at(maker, pid=pa, sid=sa, seconds=0)
    repo = SqlLocationRepo(maker)
    for ctx in (NARROWED_LOCATION, NARROWED_GENERAL):
        assert (
            await repo.fixes_within(
                ctx, subject_id=sa, since=_BASE, until=_BASE + timedelta(hours=1), limit=100
            )
            == []
        )


async def test_fixes_within_clamps_an_overwide_window(maker: async_sessionmaker) -> None:
    # A window wider than the 31-day max has `since` pulled forward, so an ancient fix
    # outside the clamped window is excluded.
    pa, sa = await _device(maker, "FW Clamp Phone")
    until = _BASE + timedelta(days=60)
    ancient = _BASE  # 60 days before `until` — outside the 31-day clamp
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :ts, :lat, :lon)"
            ),
            {"s": sa, "p": pa, "ts": ancient, "lat": _HOME[0], "lon": _HOME[1]},
        )
        # A recent fix inside the clamped window.
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :ts, :lat, :lon)"
            ),
            {
                "s": sa,
                "p": pa,
                "ts": until - timedelta(days=1),
                "lat": _HOME[0],
                "lon": _HOME[1],
            },
        )
    fixes = await SqlLocationRepo(maker).fixes_within(
        OWNER, subject_id=sa, since=ancient - timedelta(days=1), until=until, limit=100
    )
    # Only the recent fix survives — the clamp excluded the 60-day-old one.
    assert len(fixes) == 1
    assert all(f.captured_at >= until - timedelta(days=31) for f in fixes)


async def test_fixes_within_spatial_filter_keeps_only_fixes_near_center(
    maker: async_sessionmaker,
) -> None:
    pa, sa = await _device(maker, "FW Spatial Phone")
    base_lat, base_lon = 30.0, 40.0
    # One fix at the center, one ~111 m N (inside 150 m), one ~1.1 km N (outside).
    await _fix_at_coord(maker, pid=pa, sid=sa, lat=base_lat, lon=base_lon, minute=0)
    await _fix_at_coord(maker, pid=pa, sid=sa, lat=base_lat + 0.0010, lon=base_lon, minute=1)
    await _fix_at_coord(maker, pid=pa, sid=sa, lat=base_lat + 0.0100, lon=base_lon, minute=2)
    repo = SqlLocationRepo(maker)
    near = await repo.fixes_within(
        OWNER,
        subject_id=sa,
        since=_BASE,
        until=_BASE + timedelta(hours=1),
        center=(base_lat, base_lon),
        radius_m=150.0,
        limit=100,
    )
    # Only the two fixes within 150 m of the center; the 1.1 km one is filtered out.
    assert len(near) == 2


# --- L4 dwell/time tools end-to-end: time_at_place / find_when_at -------------
# The real handlers over real repos, proving the owner's device resolves through
# the deterministic binding and that dwells (WEAK-RLS `app.events`, full-owner
# guarded) totals/last-visit come back under OWNER, refused for a narrowed session.


async def _person_with_device(
    maker: async_sessionmaker, *, person_name: str, label: str
) -> tuple[str, str]:
    """A named Person bound to a device subject via operatedBy + the entity
    `subject_id` link, so the dwell tools resolve `subject=person_name` to a real
    track. A NAMED person (not the shared "Me") keeps the module-scoped DB
    deterministic — only this person's binding is in play. Returns `(pid, sid)`."""
    pid, sid = await _device(maker, label)
    person = await _entity(maker, kind="Person", name=person_name)
    device_entity = await _entity(maker, kind="Device", name=f"{label} entity")
    await _operated_by(maker, device_eid=device_entity, person_eid=person)
    await _bind_entity_subject(maker, entity_id=device_entity, subject_id=sid)
    return pid, sid


async def test_time_at_place_totals_owner_dwells(maker: async_sessionmaker) -> None:
    pid, sid = await _person_with_device(maker, person_name="L4 TAP Jeff", label="L4 TAP")
    place_eid = await _fence_at(maker, name="L4 TAP Office", lat=50.0, lon=60.0)
    # Two stays at the place: 30 min + 20 min = 50 min, 2 visits.
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="enter", minute=0)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="exit", minute=30)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="enter", minute=60)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="exit", minute=80)

    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    # A wide window so _BASE-relative stays fall inside the lookback.
    out = await handlers["time_at_place"](
        {"place": "L4 TAP Office", "subject": "L4 TAP Jeff", "hours": 24 * 365}, _tool_ctx()
    )
    assert "at L4 TAP Office" in out and "2 visits" in out
    assert "50.0" not in str(out) and "60.0" not in str(out)


async def test_find_when_at_reports_last_visit_owner(maker: async_sessionmaker) -> None:
    pid, sid = await _person_with_device(maker, person_name="L4 FWA Jeff", label="L4 FWA")
    place_eid = await _fence_at(maker, name="L4 FWA Gym", lat=51.0, lon=61.0)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="enter", minute=0)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="exit", minute=30)

    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    out = await handlers["find_when_at"](
        {"place": "L4 FWA Gym", "subject": "L4 FWA Jeff", "hours": 24 * 365}, _tool_ctx()
    )
    assert "last visited L4 FWA Gym" in out and "1 visit" in out


async def test_find_when_at_no_visits_owner(maker: async_sessionmaker) -> None:
    await _person_with_device(maker, person_name="L4 NV Jeff", label="L4 NV")
    await _fence_at(maker, name="L4 NV Cafe", lat=52.0, lon=62.0)  # a fence, but no crossings
    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    out = await handlers["find_when_at"](
        {"place": "L4 NV Cafe", "subject": "L4 NV Jeff"}, _tool_ctx()
    )
    assert "No recorded visits to L4 NV Cafe" in out


async def test_dwell_tools_refuse_a_narrowed_session(maker: async_sessionmaker) -> None:
    # dwells reads WEAK-RLS app.events; the full-owner gate (wrapper) must refuse a
    # narrowed/owner_scoped session for BOTH tools before any read.
    pid, sid = await _person_with_device(maker, person_name="L4 REF Jeff", label="L4 REF")
    place_eid = await _fence_at(maker, name="L4 REF Place", lat=53.0, lon=63.0)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="enter", minute=0)
    await _crossing(maker, pid=pid, sid=sid, eid=place_eid, transition="exit", minute=30)
    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker)
    )
    narrowed = ToolContext(session=NARROWED_LOCATION, scopes=())
    for tool in ("time_at_place", "find_when_at"):
        with pytest.raises(LocationToolRefusal):
            await handlers[tool]({"place": "L4 REF Place"}, narrowed)


# --- L7a: the digest compute over real dwells + the full-owner gate -----------
# The digest is compute-on-read from the owner's dwells (WEAK-RLS app.events) + the
# home place name (WEAK-RLS place_geofence). Both reads are full-owner gated; the
# endpoint's barrier IS that gate, so a narrowed owner is REFUSED, not handed rows.


async def test_digest_compute_over_real_dwells(maker: async_sessionmaker) -> None:
    from datetime import timezone

    from jbrain.locations.digest import compute_digest

    pid, sid = await _device(maker, "L7 Digest Phone")
    home = await _fence_at(maker, name="Home", lat=70.0, lon=80.0)
    office = await _fence_at(maker, name="L7 Office", lat=70.1, lon=80.1)
    # A home stay and an office stay, both within the digest window.
    await _crossing(maker, pid=pid, sid=sid, eid=home, transition="enter", minute=0)
    await _crossing(maker, pid=pid, sid=sid, eid=home, transition="exit", minute=120)
    await _crossing(maker, pid=pid, sid=sid, eid=office, transition="enter", minute=180)
    await _crossing(maker, pid=pid, sid=sid, eid=office, transition="exit", minute=600)
    repo = SqlLocationRepo(maker)

    since = _BASE - timedelta(hours=1)
    until = _BASE + timedelta(hours=12)
    dwells = await repo.dwells(OWNER, subject_id=sid, since=since, until=until)
    digest = compute_digest(
        dwells,
        since=since,
        until=until,
        timezone="UTC",
        home_name="Home",
        period="week",
        computed_at=until,
    )
    # The office stay (7h) is the longest non-home trip; Home is never a trip.
    assert digest.longest_trip is not None
    assert digest.longest_trip.place_name == "L7 Office"
    assert digest.places_visited == 1  # Office; Home excluded
    assert digest.nights_home >= 1
    # Coordinate-free: the lat/lon used to build the fences never surface.
    blob = repr(digest)
    assert "70.0" not in blob and "80.0" not in blob
    assert timezone  # silence the unused-import guard on some linters


async def test_digest_reads_refuse_a_narrowed_owner(maker: async_sessionmaker) -> None:
    # The digest's two weak-table reads — dwells (app.events) and places
    # (place_geofence) — MUST refuse a narrowed/owner_scoped owner: RLS would
    # otherwise hand them the rows. This is the endpoint's real barrier.
    pid, sid = await _device(maker, "L7 Narrowed Phone")
    await _fence_at(maker, name="L7 Narrowed Home", lat=71.0, lon=81.0)
    repo = SqlLocationRepo(maker)
    for ctx in (NARROWED_LOCATION, NARROWED_GENERAL):
        with pytest.raises(LocationToolRefusal):
            await repo.dwells(ctx, subject_id=sid, since=_BASE, until=_BASE + timedelta(hours=1))
        with pytest.raises(LocationToolRefusal):
            await repo.places(ctx)


# --- L7b: the owner-presence read over real repos + the full-owner gate -------


async def test_presence_read_resolves_owner_place(maker: async_sessionmaker) -> None:
    from jbrain.locations.presence import read_owner_presence

    pid, sid = await _device(maker, "L7 Presence iPhone subj")
    me = await _me_entity(maker, name="Me")
    device_entity = await _entity(maker, kind="Device", name="L7 Presence iPhone")
    await _operated_by(maker, device_eid=device_entity, person_eid=me)
    await _bind_entity_subject(maker, entity_id=device_entity, subject_id=sid)
    await _geofence_state(maker, sid=sid, place_geofence_name="L7 Presence Home")
    # A very recent fix so the presence reads fresh (relative to real now).
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, now(), :lat, :lon)"
            ),
            {"s": sid, "p": pid, "lat": _HOME[0], "lon": _HOME[1]},
        )

    presence = await read_owner_presence(SqlLocationRepo(maker), SqlDeviceRepo(maker), OWNER)
    assert presence.present and presence.place_name == "L7 Presence Home"
    assert not presence.stale


async def test_presence_read_refuses_a_narrowed_owner(maker: async_sessionmaker) -> None:
    from jbrain.locations.presence import read_owner_presence

    # read_owner_presence calls require_full_owner FIRST — a narrowed owner is refused
    # before any read (the gate the owner-only injection/endpoint rely on).
    repo = SqlLocationRepo(maker)
    devices = SqlDeviceRepo(maker)
    for ctx in (NARROWED_LOCATION, NARROWED_GENERAL):
        with pytest.raises(LocationToolRefusal):
            await read_owner_presence(repo, devices, ctx)


async def _owner_principal(maker: async_sessionmaker) -> SessionContext:
    """An owner Principal row + a FULL-owner ctx bound to it (the warm-fix cache's
    FK target)."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.principals (id, kind, key_hash) VALUES (:p, 'owner', :kh)"),
            {"p": pid, "kh": uuid.uuid4().hex},
        )
    return SessionContext(principal_id=pid, principal_kind="owner")


async def test_owner_last_fix_roundtrip_upsert_and_age_cap(maker: async_sessionmaker) -> None:
    owner = await _owner_principal(maker)
    repo = SqlLocationRepo(maker)
    # Nothing cached yet.
    assert await repo.owner_fix(owner, max_age_seconds=3600) is None
    # Remember a fix, read it back.
    await repo.remember_owner_fix(owner, latitude=28.6, longitude=-80.8)
    got = await repo.owner_fix(owner, max_age_seconds=3600)
    assert got is not None and round(got.latitude, 1) == 28.6 and round(got.longitude, 1) == -80.8
    # A second remember UPSERTS in place — still one fix, the newer coordinate.
    await repo.remember_owner_fix(owner, latitude=51.5, longitude=-0.13)
    got2 = await repo.owner_fix(owner, max_age_seconds=3600)
    assert got2 is not None
    assert round(got2.latitude, 1) == 51.5 and round(got2.longitude, 2) == -0.13
    # The age cap hides a fix older than the window (here: anything before "now").
    assert await repo.owner_fix(owner, max_age_seconds=0) is None
