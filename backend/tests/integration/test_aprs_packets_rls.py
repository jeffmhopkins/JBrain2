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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.sdr import aprslog
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


# --- Retention -------------------------------------------------------------------
#
# These run against real Postgres rather than a fake because the property that matters
# is one a fake cannot have: the table is `app.is_owner()`-gated with FORCE ROW LEVEL
# SECURITY, so a prune running under the wrong context deletes NOTHING and returns
# cleanly. That is a retention job that looks healthy and lets the table grow for ever,
# and only a real policy can catch it.

_INSERT_AT = text(
    "INSERT INTO app.aprs_packets"
    " (heard_at, frequency_hz, source, destination, path, info, raw, origin_call, addressee)"
    " VALUES (:at, :hz, :src, :dst, :path, :info, :raw, :origin, :addressee)"
)


async def _put(
    maker: async_sessionmaker,
    *,
    days_ago: float,
    origin: str | None = None,
    addressee: str | None = None,
    source: str = "RELAY-1",
) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            _INSERT_AT,
            {
                **_ROW,
                "at": datetime.now(tz=UTC) - timedelta(days=days_ago),
                "src": source,
                "origin": origin,
                "addressee": addressee,
            },
        )
        await s.commit()


async def _calls(maker: async_sessionmaker) -> list[tuple[str | None, str | None]]:
    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT origin_call, addressee FROM app.aprs_packets"))).all()
    return sorted((r.origin_call, r.addressee) for r in rows)


@pytest.fixture
async def empty_log(maker: async_sessionmaker) -> AsyncIterator[None]:
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.aprs_packets"))
        await s.commit()


async def test_prune_removes_old_traffic_and_keeps_recent(
    maker: async_sessionmaker, empty_log: None
) -> None:
    await _put(maker, days_ago=20, origin="W1AW")
    await _put(maker, days_ago=1, origin="W1AW")

    removed = await aprslog.prune(maker, OWNER, owner_call=None)

    # The row count is asserted, not just the return: a prune that reports 1 and deleted
    # nothing is precisely the RLS failure this file exists to catch.
    assert removed == 1
    assert await _calls(maker) == [("W1AW", None)]


async def test_prune_keeps_the_owners_own_transmissions(
    maker: async_sessionmaker, empty_log: None
) -> None:
    await _put(maker, days_ago=30, origin="KD4ABC")
    await _put(maker, days_ago=30, origin="W1AW")

    removed = await aprslog.prune(maker, OWNER, owner_call="KD4ABC")

    assert removed == 1
    assert await _calls(maker) == [("KD4ABC", None)]


async def test_prune_keeps_mail_addressed_to_the_owner(
    maker: async_sessionmaker, empty_log: None
) -> None:
    # A message sent TO the owner is theirs as much as one they sent: it is mail, and
    # mail should not evaporate because a fortnight passed.
    await _put(maker, days_ago=30, origin="W1AW", addressee="KD4ABC")
    await _put(maker, days_ago=30, origin="W1AW", addressee="N0CALL")

    removed = await aprslog.prune(maker, OWNER, owner_call="KD4ABC")

    assert removed == 1
    assert await _calls(maker) == [("W1AW", "KD4ABC")]


async def test_prune_keeps_an_unclassified_row_the_owner_sent(
    maker: async_sessionmaker, empty_log: None
) -> None:
    # The backfill sweep has not reached this row, so `origin_call` is NULL and the only
    # evidence it is the owner's is `source`. Without the COALESCE the owner's own
    # packets are pruned while the backlog is still filling in — the one case where the
    # exemption silently does not apply.
    await _put(maker, days_ago=30, origin=None, source="KD4ABC")

    removed = await aprslog.prune(maker, OWNER, owner_call="KD4ABC")

    assert removed == 0
    assert await _calls(maker) == [(None, None)]


async def test_prune_with_no_callsign_set_exempts_nothing(
    maker: async_sessionmaker, empty_log: None
) -> None:
    # Stated as a test because it is a real state: an owner who has not set a callsign
    # on the Radio screen keeps nothing, and that should be a decision rather than a
    # surprise.
    await _put(maker, days_ago=30, origin="KD4ABC")

    removed = await aprslog.prune(maker, OWNER, owner_call=None)

    assert removed == 1
    assert await _calls(maker) == []
