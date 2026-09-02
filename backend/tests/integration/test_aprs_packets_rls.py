"""Migration 0180 against real Postgres: `app.aprs_packets` is owner-only.

The radio is a physical device on the owner's box, so what it overheard has no
scoped-token or family case at all — unlike most tables here, this one has no
"filtered view for a narrower scope". A non-owner sees an EMPTY table and cannot
write, and the enforcement is Postgres rather than the caller (CLAUDE.md rule 3).

Worth stating what this protects. The log holds third parties' traffic heard off a
shared channel, and `info` is untrusted text from anyone with a transmitter. A scoped
token leaking it would expose other operators' messages and the owner's own command
words — which are exactly the strings an attacker would want before trying to forge
one (docs/plans/APRS_CONTROL_PLAN.md, the two trust tiers).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)

_INSERT = text(
    "INSERT INTO app.aprs_packets (frequency_hz, source, destination, path, info, raw)"
    " VALUES (:hz, :src, :dst, :path, :info, :raw)"
)
_ROW = {
    "hz": 144_390_000,
    "src": "KE8XYZ-9",
    "dst": "APDW17",
    "path": ["WIDE1-1"],
    "info": "GATE 7K2M9",
    "raw": "deadbeef",
}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def one_packet(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_INSERT, _ROW)
        await s.commit()
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.aprs_packets"))
        await s.commit()


async def test_the_owner_reads_what_the_radio_heard(
    maker: async_sessionmaker, one_packet: None
) -> None:
    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT source, info FROM app.aprs_packets"))).all()

    assert [(r.source, r.info) for r in rows] == [("KE8XYZ-9", "GATE 7K2M9")]


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_sees_nothing(
    maker: async_sessionmaker, one_packet: None, ctx_name: str
) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT id FROM app.aprs_packets"))).all()

    # EVERY_SCOPE is the interesting one: holding all four domain scopes is still not
    # being the owner, and heard radio traffic is not domain-scoped data.
    assert rows == []


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_cannot_write(maker: async_sessionmaker, ctx_name: str) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    # Forging a heard packet is forging the evidence a command path reads.
    with pytest.raises((ProgrammingError, DBAPIError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(_INSERT, _ROW)
            await s.commit()


async def test_a_non_owner_cannot_delete_the_log(
    maker: async_sessionmaker, one_packet: None
) -> None:
    async with scoped_session(maker, GENERAL_ONLY) as s:
        await s.execute(text("DELETE FROM app.aprs_packets"))
        await s.commit()

    # RLS makes the DELETE match no rows rather than error, so the check is that the
    # row SURVIVED — an attacker covering their tracks is the thing being stopped.
    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT id FROM app.aprs_packets"))).all()
    assert len(rows) == 1
