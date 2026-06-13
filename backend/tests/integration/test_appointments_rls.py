"""Migration 0026 against real Postgres: the appointments firewall (CLAUDE.md
rule 3).

An appointment is a projection of an appointment entity — owner-only and
single-domain. A health appointment is invisible to a finance-scoped session and
to any non-owner principal (#7/#8); deleting the projected entity cascades the
row away (the purge promise).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
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


async def _make_entity(maker: async_sessionmaker, code: str, name: str) -> str:
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


async def _make_appt(maker: async_sessionmaker, code: str, title: str) -> tuple[str, str]:
    eid = await _make_entity(maker, code, title)
    async with scoped_session(maker, OWNER) as session:
        aid = (
            await session.execute(
                text(
                    "INSERT INTO app.appointments (domain_code, entity_id, title, starts_at)"
                    " VALUES (:code, :eid, :title, :when) RETURNING id"
                ),
                {"code": code, "eid": eid, "title": title, "when": _WHEN},
            )
        ).scalar()
    return str(aid), eid


async def test_appointments_owner_only_and_domain_narrowed(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    for code in ("general", "health", "finance"):
        await _make_appt(maker, code, f"{tag} {code}")
    like = {"t": f"{tag}%"}

    # A health-only owner session sees only the health appointment.
    health = read_context(pid, ("health",))
    async with scoped_session(maker, health) as session:
        rows = list(
            (
                await session.execute(
                    text("SELECT domain_code FROM app.appointments WHERE title LIKE :t"), like
                )
            ).scalars()
        )
    assert rows == ["health"]

    # The unnarrowed owner sees all three; a non-owner sees none (#8).
    async with scoped_session(maker, OWNER) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.appointments WHERE title LIKE :t"), like
            )
        ).scalar() == 3
    token = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
    async with scoped_session(maker, token) as session:
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.appointments WHERE title LIKE :t"), like
            )
        ).scalar() == 0


async def test_narrowed_owner_cannot_write_outside_scope(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    # The entity exists (the owner made it); a health-scoped session still cannot
    # project an appointment into finance — the RLS WITH CHECK rejects it.
    eid = await _make_entity(maker, "finance", "sneaky")
    health = read_context(pid, ("health",))
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, health) as session:
            await session.execute(
                text(
                    "INSERT INTO app.appointments (domain_code, entity_id, title, starts_at)"
                    " VALUES ('finance', :eid, 'sneaky', :when)"
                ),
                {"eid": eid, "when": _WHEN},
            )


async def test_purging_the_entity_cascades_the_appointment(maker: async_sessionmaker) -> None:
    await _owner_principal(maker)
    tag = uuid.uuid4().hex[:8]
    aid, eid = await _make_appt(maker, "health", f"{tag} dentist")
    exists = text("SELECT count(*) FROM app.appointments WHERE id = :aid")

    async with scoped_session(maker, OWNER) as session:
        assert (await session.execute(exists, {"aid": aid})).scalar() == 1
        # Deleting the projected entity removes its appointment (ON DELETE CASCADE)
        # — the privacy promise that nothing derived from a purged note survives.
        await session.execute(text("DELETE FROM app.entities WHERE id = :eid"), {"eid": eid})
        assert (await session.execute(exists, {"aid": aid})).scalar() == 0


async def test_appointment_status_is_constrained(maker: async_sessionmaker) -> None:
    await _owner_principal(maker)
    eid = await _make_entity(maker, "general", "bad-status")
    # The CHECK mirrors the appointment.yaml Lifecycle enum — a typo is rejected.
    with pytest.raises(IntegrityError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(
                text(
                    "INSERT INTO app.appointments"
                    " (domain_code, entity_id, title, starts_at, status)"
                    " VALUES ('general', :eid, 'x', :when, 'maybe')"
                ),
                {"eid": eid, "when": _WHEN + timedelta(days=1)},
            )
