"""The classifier sweep against real Postgres — migration 0185's columns, filled in.

Two halves of one promise. The live path classifies a frame as it stores it, so the log
is filterable the moment it is written; the sweep re-derives rows that have no
classification yet — the ones written before the columns existed, and any row a later,
better classifier would read differently. Both run the SAME `derive`, which is what makes
"a wrong derivation costs a re-run, never a row" true.

Real Postgres rather than a fake because everything that can be wrong here is SQL: the
INSERT's bind parameters, the UPDATE's, and the `kind IS NULL` predicate the sweep claims
rows with. A fake session would have agreed with all three while the live insert failed
inside a handler that swallows its error to a log line.

Every frame quoted is real, captured on 144.390 near Titusville.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.sdr.aprslog import AprsLog, _parse
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The finding the whole feature turns on: the AX.25 source is the IGATE, and the station
# that composed this is N1MPR-C. Three quarters of the measured capture looked like this.
GATED = "}N1MPR-C>APDG02,TCPIP,N4TDX*:!2835.06ND08048.98W&RNG0001/A=000010 2m Voice"
# Heard directly off the air — the KSC club digipeater's own beacon.
DIRECT = "@281607z2835.13N/08039.04WSPLXDigi U=14.2V. KSC Amateur Radio Club"

# The pre-0185 INSERT, verbatim: rows that exist on the owner's box right now.
_OLD_INSERT = text(
    "INSERT INTO app.aprs_packets (frequency_hz, source, destination, path, info, raw)"
    " VALUES (:hz, :src, :dst, :path, :info, :raw)"
)


def _old_row(src: str, info: str, path: list[str]) -> dict[str, Any]:
    return {
        "hz": 144_390_000,
        "src": src,
        "dst": "APRS",
        "path": path,
        "info": info,
        "raw": "",
    }


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.aprs_packets"))
        await s.commit()


async def _rows(maker: async_sessionmaker) -> list[dict[str, Any]]:
    async with scoped_session(maker, OWNER) as s:
        result = (
            await s.execute(
                text(
                    "SELECT source, origin_call, data_type, kind, gated, heard_direct,"
                    " addressee FROM app.aprs_packets ORDER BY info"
                )
            )
        ).mappings()
        return [dict(r) for r in result]


async def test_rows_written_before_the_columns_existed_are_classified(
    maker: async_sessionmaker, clean: None
) -> None:
    """The sweep's whole reason to exist.

    The owner's box has been logging since P1 shipped. Those rows are not disposable —
    they are the capture this feature was designed from — and they must become
    filterable without anyone touching a terminal (CLAUDE.md rule 10)."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_OLD_INSERT, _old_row("N4TDX", GATED, ["N1KSC-1*"]))
        await s.execute(_OLD_INSERT, _old_row("N1KSC-1", DIRECT, []))
        await s.commit()

    filled = await AprsLog(maker=maker, base_url="").backfill()

    assert filled == 2
    direct, gated = sorted(await _rows(maker), key=lambda r: r["source"])
    # The relay stays in `source` — nothing is rewritten — and the true sender appears
    # beside it. That is what makes a station roster mean anything.
    assert (gated["source"], gated["origin_call"]) == ("N4TDX", "N1MPR-C")
    assert (gated["kind"], gated["gated"], gated["heard_direct"]) == ("Position", True, False)
    assert (direct["source"], direct["origin_call"]) == ("N1KSC-1", "N1KSC-1")
    assert (direct["kind"], direct["gated"], direct["heard_direct"]) == ("Position", False, True)


async def test_a_second_sweep_finds_nothing(maker: async_sessionmaker, clean: None) -> None:
    # The sweep runs forever on a minute timer. If it re-claimed classified rows it would
    # rewrite the whole table every minute for as long as the box is up.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_OLD_INSERT, _old_row("N4TDX", GATED, []))
        await s.commit()

    assert await AprsLog(maker=maker, base_url="").backfill() == 1
    assert await AprsLog(maker=maker, base_url="").backfill() == 0


async def test_the_sweep_has_an_index_that_matches_its_own_predicate(
    maker: async_sessionmaker, clean: None
) -> None:
    """Nothing to do is the steady state, and it has to stay cheap.

    The table grows by roughly 3,000 rows a day on the measured channel and the sweep
    asks its question every minute forever. The partial index is what keeps that free:
    once every row is classified the index is EMPTY. The predicate here must match the
    sweep's WHERE exactly — a partial index Postgres cannot prove applies is a partial
    index it will not use."""
    assert await AprsLog(maker=maker, base_url="").backfill() == 0

    async with scoped_session(maker, OWNER) as s:
        ddl = (
            await s.execute(
                text(
                    "SELECT indexdef FROM pg_indexes"
                    " WHERE schemaname = 'app' AND indexname = 'aprs_packets_unclassified_idx'"
                )
            )
        ).scalar_one()

    assert "(kind IS NULL)" in ddl
    assert "heard_at DESC" in ddl


async def test_a_freshly_stored_frame_is_already_classified(
    maker: async_sessionmaker, clean: None
) -> None:
    """The live path, end to end: the sidecar's line, through the parser, into Postgres.

    This is the test that would have caught a bind parameter missing from the INSERT.
    `_store` swallows its errors so one bad row cannot end the drain, so a broken
    statement does not raise anywhere — the log simply stops recording, silently."""
    row = _parse(
        json.dumps(
            {
                "source": "N1KSC-1",
                "destination": "APRS",
                "path": ["N1KSC-1*"],
                "info": GATED,
                "raw": "",
                "frequency_hz": 144_390_000,
                "heard_at": 1_760_000_000.5,
            }
        )
    )
    assert row is not None

    await AprsLog(maker=maker, base_url="")._store(row)

    stored = await _rows(maker)
    assert len(stored) == 1
    assert stored[0]["origin_call"] == "N1MPR-C"
    assert stored[0]["kind"] == "Position"
    # And the sweep does not then re-claim it.
    assert await AprsLog(maker=maker, base_url="").backfill() == 0


async def test_a_message_keeps_its_addressee(maker: async_sessionmaker, clean: None) -> None:
    # How the owner's own mail is found. It arrives WRAPPED, because an IGate relays a
    # message to RF only when the addressee has been heard nearby — so reading the
    # addressee off the outer frame would find nothing.
    relayed = "}W4XYZ>APRS,TCPIP,N4TDX*::KE8XYZ   :on my way{007"
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_OLD_INSERT, _old_row("N4TDX", relayed, []))
        await s.commit()

    await AprsLog(maker=maker, base_url="").backfill()

    (row,) = await _rows(maker)
    assert (row["kind"], row["addressee"], row["origin_call"]) == ("Message", "KE8XYZ", "W4XYZ")


async def test_a_row_the_classifier_cannot_read_is_left_for_the_next_sweep(
    maker: async_sessionmaker, clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NULL `kind` is a claim check, not a broken row.

    `classify` is total by construction, so reaching this is a bug in it. Writing a
    placeholder anyway would be worse than leaving the row: the sweep would never look
    at it again, so the classifier fix would silently skip exactly the rows it was
    written for. Leaving it costs one bounded query a minute, against an index that
    holds only the rows still waiting."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_OLD_INSERT, _old_row("N4TDX", GATED, []))
        await s.commit()
    monkeypatch.setattr(
        "jbrain.sdr.aprslog.classify",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert await AprsLog(maker=maker, base_url="").backfill() == 0

    (row,) = await _rows(maker)
    assert row["kind"] is None
    # The frame itself is untouched, so the fixed classifier gets the same bytes.
    monkeypatch.undo()
    assert await AprsLog(maker=maker, base_url="").backfill() == 1
    assert (await _rows(maker))[0]["origin_call"] == "N1MPR-C"
