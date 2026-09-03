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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.sdr import aprslog as aprslog_module
from jbrain.sdr.aprslog import BACKFILL_CLAIM_SQL, AprsLog, AprsReader, _parse
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


async def test_the_sweep_actually_uses_its_partial_index(
    maker: async_sessionmaker, clean: None
) -> None:
    """Nothing to do is the steady state, and it has to stay cheap.

    The table grows by roughly 3,000 rows a day on the measured channel and the sweep
    asks its question every minute forever. The partial index is what keeps that free:
    once every row is classified the index is EMPTY, so the sweep reads nothing at all.

    This EXPLAINs the statement the sweep RUNS, not the index's own definition. An index
    whose predicate merely looks similar is an index Postgres will decline to use, and a
    test that greps `pg_indexes` would call that a pass."""
    now = datetime.now(UTC)
    async with scoped_session(maker, OWNER) as s:
        # Enough rows that a sequential scan is not simply the cheaper plan, with a few
        # needles: the steady state this has to be free in is a big, fully-derived table.
        await s.execute(
            text(
                "INSERT INTO app.aprs_packets (heard_at, frequency_hz, source,"
                " destination, path, info, raw, origin_call, kind)"
                " SELECT (:now)::timestamptz - (g || ' seconds')::interval, 144390000,"
                " 'W4XYZ', 'APRS',"
                " '{}', :info, '', 'W4XYZ', 'Position' FROM generate_series(1, 4000) g"
            ),
            {"now": now, "info": DIRECT},
        )
        await s.execute(_OLD_INSERT, _old_row("N4TDX", GATED, []))
        await s.execute(text("ANALYZE app.aprs_packets"))
        plan = " ".join(
            (await s.execute(text("EXPLAIN " + BACKFILL_CLAIM_SQL), {"batch": 200})).scalars()
        )

    assert "aprs_packets_unclassified_idx" in plan, plan
    assert "Seq Scan" not in plan, plan


async def test_the_sweep_takes_the_newest_unclassified_rows_first(
    maker: async_sessionmaker, clean: None
) -> None:
    """A backlog is worked from the top down, so the screen fills in where the owner is
    looking. With a batch smaller than the backlog, the order is the whole behaviour."""
    now = datetime.now(UTC)
    async with scoped_session(maker, OWNER) as s:
        for hours, info in ((5, GATED), (1, DIRECT)):
            await s.execute(
                text(
                    "INSERT INTO app.aprs_packets (heard_at, frequency_hz, source,"
                    " destination, path, info, raw) VALUES (:t, 144390000, :src,"
                    " 'APRS', '{}', :info, '')"
                ),
                {"t": now - timedelta(hours=hours), "src": "N4TDX", "info": info},
            )
        await s.commit()

    assert await AprsLog(maker=maker, base_url="").backfill(batch=1) == 1

    classified = [r for r in await _rows(maker) if r["kind"]]
    assert [r["origin_call"] for r in classified] == ["N4TDX"]  # the newer, direct one


async def test_one_unstorable_row_does_not_stall_the_whole_sweep(
    maker: async_sessionmaker, clean: None
) -> None:
    """The batch is claimed from a channel anyone can transmit on.

    Before the per-row savepoint, a single row Postgres refused aborted the entire
    statement — every row in the batch stayed unclassified, and the sweep re-selected the
    same poisoned batch every minute forever. One frame, once, would have permanently
    stopped the archive from ever being classified, and the only trace is a log line on a
    box the owner has no terminal for (CLAUDE.md rule 10)."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_OLD_INSERT, _old_row("N4TDX", GATED, []))
        await s.execute(_OLD_INSERT, _old_row("N1KSC-1", DIRECT, []))
        await s.commit()

    # A derivation that is fine in Python and unstorable in Postgres — the shape a
    # crafted frame produces, forced here rather than hoping a byte still slips through.
    real = aprslog_module.derive

    def poison(row: dict[str, object]) -> dict[str, object]:
        values = real(row)
        if values["origin_call"] == "N1MPR-C":
            values["data_type"] = "\x00"
        return values

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(aprslog_module, "derive", poison)
    try:
        filled = await AprsLog(maker=maker, base_url="").backfill()
    finally:
        monkeypatched.undo()

    # The good row landed; only the bad one was left behind.
    assert filled == 1
    by_kind = {r["origin_call"]: r["kind"] for r in await _rows(maker)}
    assert by_kind["N1KSC-1"] == "Position"
    assert by_kind[None] is None  # the poisoned row is still waiting, not lost


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


async def test_a_frame_whose_info_begins_with_a_nul_is_still_recorded(
    maker: async_sessionmaker, clean: None
) -> None:
    """The whole promise, at the one point it was breakable.

    `raw` is stored losslessly so a classifier bug costs a re-run and never a row — but
    `data_type` is the one derived column read from `raw` rather than from the scrubbed
    `info`, so a frame whose info field simply starts with 0x00 used to carry that byte
    into a text column. Postgres refuses it, `_store` swallows the error to keep the
    drain alive, and the packet vanishes with the `raw` that would have recovered it.
    A NUL-first info field is a perfectly forwardable AX.25 UI frame: this is reachable
    by anyone with a transmitter."""
    dest = bytes([ord(c) << 1 for c in "APRS  "]) + bytes([0x60])
    src = bytes([ord(c) << 1 for c in "N0CALL"]) + bytes([0x61])
    frame = dest + src + bytes([0x03, 0xF0]) + b"\x00!2835.13N/08039.04W-"
    row = _parse(
        json.dumps(
            {
                "source": "N0CALL",
                "destination": "APRS",
                "path": [],
                # As the scrub leaves it: the NUL is gone from `info` and still in `raw`.
                "info": "!2835.13N/08039.04W-",
                "raw": frame.hex(),
                "frequency_hz": 144_390_000,
                "heard_at": 1_760_000_000.5,
            }
        )
    )
    assert row is not None

    await AprsLog(maker=maker, base_url="")._store(row)

    (stored,) = await _rows(maker)
    assert stored["origin_call"] == "N0CALL"
    assert stored["kind"] == "Position"


async def _store(maker: async_sessionmaker, payload: dict[str, Any]) -> None:
    row = _parse(json.dumps({"frequency_hz": 144_390_000, "raw": "", **payload}))
    assert row is not None
    await AprsLog(maker=maker, base_url="")._store(row)


class TestReadingTheLogBack:
    """`AprsReader`, against real SQL.

    The unit tests fake the reader, so nothing there can catch a WHERE clause that
    selects the wrong rows — and two deliberate mutations survived the whole unit suite
    for exactly that reason. These are the ones that need Postgres."""

    async def test_station_finds_the_sender_inside_a_relayed_frame(
        self, maker: async_sessionmaker, clean: None
    ) -> None:
        """The question the tool exists to answer. `GATED`'s AX.25 source is the IGate,
        so a filter on `source` reports N1MPR-C as never heard."""
        await _store(maker, {"source": "N4TDX", "destination": "APRS", "info": GATED})

        heard = await AprsReader(maker).recent(OWNER, station="N1MPR-C")
        by_source = await AprsReader(maker).recent(OWNER, source="N1MPR-C")

        assert len(heard) == 1
        assert by_source == []

    async def test_station_still_finds_a_row_the_sweep_has_not_reached(
        self, maker: async_sessionmaker, clean: None
    ) -> None:
        """`origin_call` is NULL until the sweep fills it in, and for a DIRECT frame the
        sender IS the AX.25 source — so without the COALESCE a station's own traffic
        reads as never heard while the backlog works. This mutation survived the unit
        suite."""
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.aprs_packets"
                    " (frequency_hz, source, destination, path, info, raw)"
                    " VALUES (144390000, 'K4KSC-1', 'APRS', '{}', :info, '')"
                ),
                {"info": DIRECT},
            )
            await s.commit()

        heard = await AprsReader(maker).recent(OWNER, station="K4KSC-1")

        assert len(heard) == 1
        assert heard[0]["origin_call"] is None  # genuinely unclassified

    async def test_the_digest_counts_per_station_and_honours_its_filters(
        self, maker: async_sessionmaker, clean: None
    ) -> None:
        """A digest ignoring `station` returns the whole channel under the heading the
        caller asked about — the other mutation the unit suite could not see."""
        await _store(maker, {"source": "N4TDX", "destination": "APRS", "info": GATED})
        await _store(maker, {"source": "N4TDX", "destination": "APRS", "info": GATED})
        await _store(maker, {"source": "K4KSC-1", "destination": "APRS", "info": DIRECT})

        everyone = await AprsReader(maker).digest(OWNER)
        just_one = await AprsReader(maker).digest(OWNER, station="N1MPR-C")

        # Grouped on the true sender, busiest first.
        assert [(r["station"], r["packets"]) for r in everyone] == [
            ("N1MPR-C", 2),
            ("K4KSC-1", 1),
        ]
        assert [(r["station"], r["packets"]) for r in just_one] == [("N1MPR-C", 2)]

    async def test_the_window_and_the_kind_narrow_what_comes_back(
        self, maker: async_sessionmaker, clean: None
    ) -> None:
        await _store(
            maker,
            {
                "source": "K4KSC-1",
                "destination": "APRS",
                "info": DIRECT,
                "heard_at": (datetime.now(UTC) - timedelta(days=3)).timestamp(),
            },
        )
        await _store(maker, {"source": "N4TDX", "destination": "APRS", "info": GATED})

        recent = await AprsReader(maker).recent(OWNER, since=datetime.now(UTC) - timedelta(hours=1))
        weather = await AprsReader(maker).recent(OWNER, kind="Weather")

        assert [r["origin_call"] for r in recent] == ["N1MPR-C"]
        assert weather == []

    async def test_the_measured_signal_comes_back_with_the_row(
        self, maker: async_sessionmaker, clean: None
    ) -> None:
        await _store(
            maker,
            {"source": "K4KSC-1", "destination": "APRS", "info": DIRECT, "audio_level": 72},
        )

        (heard,) = await AprsReader(maker).recent(OWNER)

        assert heard["audio_level"] == 72

    async def test_a_level_outside_direwolf_s_range_never_reaches_the_table(
        self, maker: async_sessionmaker, clean: None
    ) -> None:
        """The CHECK constraint is the backstop, but the parser has to drop it FIRST:
        `_store` swallows its errors to keep the drain alive, so a rejected insert would
        cost the whole packet — the frame, the raw bytes, everything — over a bad
        number in one column."""
        await _store(
            maker,
            {"source": "K4KSC-1", "destination": "APRS", "info": DIRECT, "audio_level": 9999},
        )

        (heard,) = await AprsReader(maker).recent(OWNER)

        assert heard["audio_level"] is None
        assert heard["info"] == DIRECT  # the row survived
