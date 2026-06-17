"""Phase 7 location firewall against real Postgres (migrations 0059-0062).

The continuous GPS stream is the most sensitive data class in JBrain2, so these
are the enforcement evidence for its stricter-than-domain firewall (CLAUDE.md
rule 3): a fix is visible only to a *full* owner OR to the very device subject it
belongs to. A device key — being a non-owner principal pinned to one subject —
can neither read another subject's track nor forge fixes for one, and the
isolation holds across TimescaleDB chunk boundaries (FORCE RLS on the parent).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.db.session import SessionContext, device_context, scoped_session
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


async def _device(maker: async_sessionmaker, name: str) -> tuple[str, str]:
    """Create a device Subject + bound device_key Principal; return (pid, sid)."""
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.subjects (id, display_name, kind) VALUES (:sid, :name, 'device')"
            ),
            {"sid": sid, "name": name},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:pid, 'device_key', :sid, :kh)"
            ),
            {"pid": pid, "sid": sid, "kh": uuid.uuid4().hex},
        )
    return pid, sid


async def _insert_fix(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    sid: str,
    pid: str | None = None,
    when: datetime,
    lat: float = 40.0,
    lon: float = -74.0,
    domain: str = "location",
) -> None:
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, domain_code, captured_at, latitude, longitude)"
                " VALUES (:sid, :pid, :dom, :ts, :lat, :lon)"
            ),
            {"sid": sid, "pid": pid, "dom": domain, "ts": when, "lat": lat, "lon": lon},
        )


async def _count(maker: async_sessionmaker, ctx: SessionContext, sql: str, args: dict) -> int:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(sql), args)).scalar() or 0


# --- location_fixes ---------------------------------------------------------


async def test_fixes_owner_sees_all_device_sees_only_its_own(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "phone A")
    pid_b, sid_b = await _device(maker, "phone B")
    when = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    await _insert_fix(maker, device_context(pid_a, sid_a), sid=sid_a, pid=pid_a, when=when)
    await _insert_fix(maker, device_context(pid_b, sid_b), sid=sid_b, pid=pid_b, when=when)

    both = "SELECT count(*) FROM app.location_fixes WHERE subject_id IN (:a, :b)"
    args = {"a": sid_a, "b": sid_b}
    # Full owner sees both subjects' fixes.
    assert await _count(maker, OWNER, both, args) == 2
    # Device A sees only its own, and cannot see device B's.
    assert await _count(maker, device_context(pid_a, sid_a), both, args) == 1
    assert (
        await _count(
            maker,
            device_context(pid_a, sid_a),
            "SELECT count(*) FROM app.location_fixes WHERE subject_id = :b",
            {"b": sid_b},
        )
        == 0
    )


async def test_fixes_invisible_to_scoped_and_unscoped_non_owners(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "phone A")
    await _insert_fix(
        maker,
        device_context(pid_a, sid_a),
        sid=sid_a,
        pid=pid_a,
        when=datetime(2026, 6, 2, 9, 0, tzinfo=UTC),
    )
    one = "SELECT count(*) FROM app.location_fixes WHERE subject_id = :a"
    args = {"a": sid_a}
    # A non-owner capability token holding the location scope still sees nothing
    # (the subject pin is unmatched), and so do other-domain / unscoped sessions.
    loc_token = SessionContext(principal_kind="capability_token", domain_scopes=("location",))
    health = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    unscoped = SessionContext(principal_kind="capability_token")
    assert await _count(maker, loc_token, one, args) == 0
    assert await _count(maker, health, one, args) == 0
    assert await _count(maker, unscoped, one, args) == 0
    # An owner-narrowed agent session scoped elsewhere is not a *full* owner.
    assert await _count(maker, read_context(str(uuid.uuid4()), ("health",)), one, args) == 0


async def test_fixes_with_check_blocks_cross_subject_and_cross_domain(
    maker: async_sessionmaker,
) -> None:
    pid_a, sid_a = await _device(maker, "phone A")
    _, sid_b = await _device(maker, "phone B")
    when = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)
    # Device A forging a fix for subject B is rejected by WITH CHECK.
    with pytest.raises(ProgrammingError):
        await _insert_fix(maker, device_context(pid_a, sid_a), sid=sid_b, pid=pid_a, when=when)
    # Device A writing into another domain (it holds only 'location') is rejected.
    with pytest.raises(ProgrammingError):
        await _insert_fix(
            maker, device_context(pid_a, sid_a), sid=sid_a, pid=pid_a, when=when, domain="health"
        )


async def test_force_rls_holds_across_hypertable_chunks(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "phone A")
    _, sid_b = await _device(maker, "phone B")
    # Fixes weeks apart land in different 7-day chunks.
    for month in (1, 2, 3):
        await _insert_fix(
            maker,
            device_context(pid_a, sid_a),
            sid=sid_a,
            pid=pid_a,
            when=datetime(2026, month, 15, 12, 0, tzinfo=UTC),
        )
    # The hypertable really did spread across multiple chunks.
    async with scoped_session(maker, OWNER) as session:
        chunks = (
            await session.execute(
                text(
                    "SELECT count(*) FROM timescaledb_information.chunks"
                    " WHERE hypertable_name = 'location_fixes'"
                )
            )
        ).scalar()
    assert chunks is not None and chunks >= 2
    one = "SELECT count(*) FROM app.location_fixes WHERE subject_id = :a"
    # Isolation still holds reading across the chunk boundary.
    assert await _count(maker, device_context(pid_a, sid_a), one, {"a": sid_a}) == 3
    assert await _count(maker, device_context("", sid_b), one, {"a": sid_a}) == 0
    unscoped = SessionContext(principal_kind="capability_token")
    assert await _count(maker, unscoped, one, {"a": sid_a}) == 0


async def test_app_role_cannot_reach_timescaledb_internal(maker: async_sessionmaker) -> None:
    # FORCE RLS on the parent is the barrier; defense in depth, the app role is
    # also never granted access to the raw chunk relations.
    async with scoped_session(maker, OWNER) as session:
        usable = (
            await session.execute(
                text("SELECT has_schema_privilege('_timescaledb_internal', 'USAGE')")
            )
        ).scalar()
    assert usable is False


# --- place_geofence (derived mirror) ---------------------------------------


async def _place(maker: async_sessionmaker, name: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Place', :name, 'location') RETURNING id"
                ),
                {"name": name},
            )
        ).scalar()
    return str(eid)


async def _make_fence(
    maker: async_sessionmaker, *, eid: str, name: str, subject_id: str | None
) -> None:
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.place_geofence"
                " (place_entity_id, subject_id, name, center, radius_m)"
                " VALUES (:eid, :sid, :name,"
                " ST_SetSRID(ST_MakePoint(-74.0, 40.0), 4326)::geography, 120)"
            ),
            {"eid": eid, "sid": subject_id, "name": name},
        )


async def test_place_geofence_read_and_write_firewall(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "phone A")
    _, sid_b = await _device(maker, "phone B")
    eid = await _place(maker, "Home")
    await _make_fence(maker, eid=eid, name="A-only", subject_id=sid_a)
    await _make_fence(maker, eid=eid, name="all-devices", subject_id=None)

    total = "SELECT count(*) FROM app.place_geofence WHERE place_entity_id = :e"
    args = {"e": eid}
    # Full owner sees both fences.
    assert await _count(maker, OWNER, total, args) == 2
    # Device A sees its own + the subject-less "all devices" fence.
    assert await _count(maker, device_context(pid_a, sid_a), total, args) == 2
    # Device B sees only the "all devices" fence, not A's.
    assert await _count(maker, device_context("", sid_b), total, args) == 1
    # A non-owner, non-device capability token (even location-scoped) sees none.
    loc_token = SessionContext(principal_kind="capability_token", domain_scopes=("location",))
    assert await _count(maker, loc_token, total, args) == 0
    # A device cannot write the mirror — only the full-owner projector may.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, device_context(pid_a, sid_a)) as session:
            await session.execute(
                text(
                    "INSERT INTO app.place_geofence (place_entity_id, subject_id, name,"
                    " center, radius_m) VALUES (:e, :s, 'sneaky',"
                    " ST_SetSRID(ST_MakePoint(-74.0, 40.0), 4326)::geography, 50)"
                ),
                {"e": eid, "s": sid_a},
            )


# --- geofence_state ---------------------------------------------------------


async def test_geofence_state_subject_firewall(maker: async_sessionmaker) -> None:
    pid_a, sid_a = await _device(maker, "phone A")
    pid_b, sid_b = await _device(maker, "phone B")
    eid = await _place(maker, "Office")
    async with scoped_session(maker, OWNER) as session:
        gid = (
            await session.execute(
                text(
                    "INSERT INTO app.place_geofence (place_entity_id, name, center, radius_m)"
                    " VALUES (:e, 'Office',"
                    " ST_SetSRID(ST_MakePoint(-74.0, 40.0), 4326)::geography, 80) RETURNING id"
                ),
                {"e": eid},
            )
        ).scalar()

    # Device A writes its own state row.
    async with scoped_session(maker, device_context(pid_a, sid_a)) as session:
        await session.execute(
            text(
                "INSERT INTO app.geofence_state (subject_id, place_geofence_id, state)"
                " VALUES (:s, :g, 'inside')"
            ),
            {"s": sid_a, "g": str(gid)},
        )
    # Device B cannot write a state row for subject A (WITH CHECK).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, device_context(pid_b, sid_b)) as session:
            await session.execute(
                text(
                    "INSERT INTO app.geofence_state (subject_id, place_geofence_id, state)"
                    " VALUES (:s, :g, 'inside')"
                ),
                {"s": sid_a, "g": str(gid)},
            )

    one = "SELECT count(*) FROM app.geofence_state WHERE subject_id = :a"
    assert await _count(maker, OWNER, one, {"a": sid_a}) == 1
    assert await _count(maker, device_context(pid_a, sid_a), one, {"a": sid_a}) == 1
    assert await _count(maker, device_context(pid_b, sid_b), one, {"a": sid_a}) == 0
