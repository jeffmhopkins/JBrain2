"""Migration 0026 against real Postgres: the appointments repo round-trip.

Appointments are a projection of appointment entities — the repo upserts one row
per entity (a reschedule updates in place, never duplicates), lists a time
window, moves the lifecycle status, and deletes. The firewall is RLS (proven in
test_appointments_rls.py); this exercises the repo's behavior.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.appointments.repo import SqlAppointmentsRepo
from jbrain.appointments.service import AppointmentSpec, UnknownDomain
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_WHEN = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _entity(maker: async_sessionmaker, code: str, name: str) -> str:
    async with scoped_session(maker, OWNER) as session:
        eid = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                    " VALUES (gen_random_uuid(), 'Event', :name, :code) RETURNING id"
                ),
                {"name": name, "code": code},
            )
        ).scalar()
    return str(eid)


async def test_upsert_is_idempotent_per_entity(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlAppointmentsRepo(maker)
    eid = await _entity(maker, "general", "Dentist")

    first = await repo.upsert(
        ctx,
        AppointmentSpec(entity_id=eid, domain="general", title="Dentist", starts_at=_WHEN),
    )
    assert first.title == "Dentist" and first.status == "confirmed" and not first.recurring

    # Re-projecting the same entity rescheduled updates in place — one row, new time.
    later = _WHEN + timedelta(days=2)
    second = await repo.upsert(
        ctx,
        AppointmentSpec(entity_id=eid, domain="general", title="Dentist (moved)", starts_at=later),
    )
    assert second.id == first.id
    assert second.starts_at == later and second.title == "Dentist (moved)"
    # Exactly one row for this entity — the upsert updated, never duplicated.
    mine = [a for a in await repo.list_appointments(ctx) if a.entity_id == eid]
    assert len(mine) == 1

    by_entity = await repo.get_by_entity(ctx, eid)
    assert by_entity is not None and by_entity.id == first.id


async def test_list_window_status_and_delete(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlAppointmentsRepo(maker)
    soon = await repo.upsert(
        ctx,
        AppointmentSpec(
            entity_id=await _entity(maker, "general", "A"),
            domain="general",
            title="A",
            starts_at=_WHEN,
        ),
    )
    far = await repo.upsert(
        ctx,
        AppointmentSpec(
            entity_id=await _entity(maker, "general", "B"),
            domain="general",
            title="B",
            starts_at=_WHEN + timedelta(days=30),
        ),
    )

    # Soonest-first ordering (assert our two rows' relative order — the shared
    # test DB may hold rows from sibling tests), and a window excludes the far one.
    ids = [a.id for a in await repo.list_appointments(ctx)]
    assert ids.index(soon.id) < ids.index(far.id)
    window = [a.id for a in await repo.list_appointments(ctx, until=_WHEN + timedelta(days=7))]
    assert soon.id in window and far.id not in window

    # A cancelled appointment drops from the default list but survives the flag.
    cancelled = await repo.set_status(ctx, far.id, "cancelled")
    assert cancelled is not None and cancelled.cancelled
    assert far.id not in [a.id for a in await repo.list_appointments(ctx)]
    assert far.id in [a.id for a in await repo.list_appointments(ctx, include_cancelled=True)]

    assert await repo.delete(ctx, soon.id) is True
    assert await repo.get_appointment(ctx, soon.id) is None


async def test_missing_ids_return_none_not_error(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlAppointmentsRepo(maker)
    assert await repo.get_appointment(ctx, "not-a-uuid") is None
    assert await repo.get_by_entity(ctx, "nope") is None
    # Both a malformed id and a valid-but-missing id are a graceful None/False.
    assert await repo.set_status(ctx, "not-a-uuid", "occurred") is None
    assert await repo.set_status(ctx, "00000000-0000-0000-0000-000000000000", "occurred") is None
    assert await repo.delete(ctx, "nope") is False


async def test_upsert_rejects_malformed_entity_id(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    with pytest.raises(ValueError, match="entity_id"):
        await SqlAppointmentsRepo(maker).upsert(
            ctx,
            AppointmentSpec(entity_id="not-a-uuid", domain="general", title="x", starts_at=_WHEN),
        )


async def test_list_since_filters_the_past(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    ctx = read_context(pid, ("general",))
    repo = SqlAppointmentsRepo(maker)
    appt = await repo.upsert(
        ctx,
        AppointmentSpec(
            entity_id=await _entity(maker, "general", "future"),
            domain="general",
            title="future",
            starts_at=_WHEN,
        ),
    )
    # `since` past the event hides it; `since` before it keeps it.
    after = [a.id for a in await repo.list_appointments(ctx, since=_WHEN + timedelta(days=1))]
    before = [a.id for a in await repo.list_appointments(ctx, since=_WHEN - timedelta(days=1))]
    assert appt.id not in after and appt.id in before


async def test_upsert_rejects_unknown_domain(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    # The session claims the bogus scope (so RLS WITH CHECK passes), but the FK to
    # app.domains rejects it — surfaced as UnknownDomain, never a raw 500.
    ctx = read_context(pid, ("bogus",))
    eid = await _entity(maker, "general", "C")
    with pytest.raises(UnknownDomain):
        await SqlAppointmentsRepo(maker).upsert(
            ctx, AppointmentSpec(entity_id=eid, domain="bogus", title="C", starts_at=_WHEN)
        )
