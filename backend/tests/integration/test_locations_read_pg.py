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

from jbrain.db.session import SessionContext, scoped_session
from jbrain.locations import SqlLocationRepo
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
    repo = SqlLocationRepo(maker)

    # has_domain_scope('location') is false for a general-only session: zero rows
    # across the fix-backed reads and the event-backed timeline.
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
    assert (
        await repo.timeline(
            NARROWED_GENERAL, since=_BASE, until=_BASE + timedelta(hours=1), limit=100
        )
        == []
    )
