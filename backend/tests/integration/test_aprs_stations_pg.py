"""The station roster against real Postgres (F2, `docs/mocks/aprs/e-stations.html`).

Real Postgres because every interesting thing here IS the SQL: the window predicates,
the `HAVING bool_or(...)` that makes the chips filter STATIONS rather than packets, and
the `(array_agg(... ORDER BY heard_at DESC))[1]` that takes each station's state from its
newest frame. A fake session would agree with all three however they were written.

The rows are seeded through the pre-classifier INSERT and then run through the real
backfill sweep, so these tests exercise the same derivation the box does rather than
asserting against hand-written column values that could quietly disagree with it.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.sdr.aprslog import AprsLog
from jbrain.sdr.stations import StationsReader
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# Real frames from the box. The first two are relayed by the IGate N4TDX — their AX.25
# source is N4TDX and their true senders are not.
GATED_POSITION = "}N1MPR-C>APDG02,TCPIP,N4TDX*:!2835.06ND08048.98W&RNG0001/A=000010 2m Voice"
GATED_WEATHER = "}KD4WLE>APRS,TCPIP,N4TDX*:@030002z2837.27N/08049.42W_317/002g002t082r000p063"
DIRECT_POSITION = "@281607z2835.13N/08039.04WSPLXDigi U=14.2V. KSC Amateur Radio Club"
DIRECT_TELEMETRY = "T#252,190,072,011,066,000,00000000"

_INSERT = text(
    "INSERT INTO app.aprs_packets (heard_at, frequency_hz, source, destination, path,"
    " info, raw) VALUES (:heard_at, 144390000, :src, 'APRS', '{}', :info, '')"
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker: async_sessionmaker, frames: list[tuple[str, str, float]]) -> None:
    """`(source, info, hours ago)` rows, then the real sweep over them."""
    now = datetime.now(UTC)
    async with scoped_session(maker, OWNER) as s:
        for src, info, hours in frames:
            await s.execute(
                _INSERT,
                {"heard_at": now - timedelta(hours=hours), "src": src, "info": info},
            )
        await s.commit()
    await AprsLog(maker=maker, base_url="").backfill()


@pytest.fixture
async def clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.aprs_packets"))
        await s.commit()


# One IGate on the air, three true senders behind it, plus a station heard directly.
_CAPTURE = [
    ("N4TDX", GATED_POSITION, 0.1),
    ("N4TDX", GATED_WEATHER, 0.2),
    ("N1KSC-1", DIRECT_POSITION, 0.3),
    ("N1KSC-1", DIRECT_TELEMETRY, 2.0),
    ("N4TDX", GATED_WEATHER, 50.0),  # two days back
    ("N4TDX", GATED_POSITION, 10 * 24),  # older than a week
]


async def test_the_roster_lists_who_sent_it_not_who_relayed_it(
    maker: async_sessionmaker, clean: None
) -> None:
    """The measurement this whole feature exists for.

    Four frames in the last day carry two AX.25 sources. Grouping on `source` would show
    two stations; grouping on the true sender shows three, and N4TDX — which composed
    nothing — does not appear at all."""
    await _seed(maker, _CAPTURE)

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    assert [s["call"] for s in roster["stations"]] == ["N1MPR-C", "KD4WLE", "N1KSC-1"]
    assert roster["stations_total"] == 3
    # Most recently heard first — the owner's correction to a roster sorted by volume,
    # which is always led by whichever machine beacons most often.
    assert roster["stations"][0]["last_heard_at"] > roster["stations"][-1]["last_heard_at"]


async def test_a_relayed_station_says_how_it_reached_us(
    maker: async_sessionmaker, clean: None
) -> None:
    await _seed(maker, _CAPTURE)

    by_call = {s["call"]: s for s in (await StationsReader(maker).roster(OWNER))["stations"]}

    # "gated via N4TDX" rather than "heard on RF" — the difference between a station in
    # the next county and one that was never on the air at all.
    assert (by_call["N1MPR-C"]["gated"], by_call["N1MPR-C"]["relay"]) == (True, "N4TDX")
    assert (by_call["N1KSC-1"]["gated"], by_call["N1KSC-1"]["relay"]) == (False, None)


async def test_the_chips_narrow_the_ROSTER_not_the_packets(
    maker: async_sessionmaker, clean: None
) -> None:
    """The distinction the owner asked for in as many words.

    Selecting Weather asks "who is putting out weather", so it returns the STATION and
    all of it — not that station's weather frames with its positions removed."""
    await _seed(maker, _CAPTURE)
    reader = StationsReader(maker)

    weather = await reader.roster(OWNER, window="1d", kinds=["Weather"])

    assert [s["call"] for s in weather["stations"]] == ["KD4WLE"]
    # The header reads "1 of 3", so the unfiltered total has to survive the filter.
    assert weather["stations_total"] == 3
    # And a station that sends two kinds is returned whole when either is selected.
    both = await reader.roster(OWNER, window="1d", kinds=["Position", "Other"])
    assert sorted(s["call"] for s in both["stations"]) == ["N1KSC-1", "N1MPR-C"]
    assert next(s for s in both["stations"] if s["call"] == "N1KSC-1")["packets"] == 2


async def test_the_chip_counts_are_stations_and_do_not_move_when_a_chip_is_pressed(
    maker: async_sessionmaker, clean: None
) -> None:
    """A chip reading 27 beside a list of three stations would be lying about what it
    does — and a chip row that rearranges itself as you use it is a control you cannot
    aim. Both counts are over the window, unfiltered by the selection."""
    await _seed(maker, _CAPTURE)
    reader = StationsReader(maker)

    plain = await reader.roster(OWNER, window="1d")
    filtered = await reader.roster(OWNER, window="1d", kinds=["Weather"])

    assert plain["kind_stations"] == {"Position": 2, "Weather": 1, "Other": 1}
    assert filtered["kind_stations"] == plain["kind_stations"]


async def test_the_windows_nest_and_older_is_the_complement(
    maker: async_sessionmaker, clean: None
) -> None:
    # "3 days" contains "1 day"; "Older" is the one exclusive bucket, and the only one
    # that can be empty while the others are full.
    await _seed(maker, _CAPTURE)

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    assert roster["window_packets"] == {"1d": 4, "3d": 5, "1w": 5, "old": 1}
    older = await StationsReader(maker).roster(OWNER, window="old")
    assert [s["call"] for s in older["stations"]] == ["N1MPR-C"]


async def test_an_unswept_row_is_reported_rather_than_silently_missing(
    maker: async_sessionmaker, clean: None
) -> None:
    """A roster missing a station has to say so.

    This is the same rule the APRS tab already follows for the receiver: a dead radio
    must not look like a quiet channel. A roster still being classified must not look
    like a channel with fewer stations on it."""
    now = datetime.now(UTC)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_INSERT, {"heard_at": now, "src": "W4XYZ", "info": DIRECT_POSITION})
        await s.commit()

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    assert roster["unclassified"] == 1
    assert roster["stations"] == []
    # And it clears once the sweep runs.
    await AprsLog(maker=maker, base_url="").backfill()
    after = await StationsReader(maker).roster(OWNER, window="1d")
    assert after["unclassified"] == 0
    assert [s["call"] for s in after["stations"]] == ["W4XYZ"]


async def test_a_station_detail_shows_the_payload_not_the_wrapper(
    maker: async_sessionmaker, clean: None
) -> None:
    """What the owner reads is the frame the station composed.

    Showing the stored `info` would print `}N1MPR-C>APDG02,TCPIP,N4TDX*:` in front of
    every relayed packet — the transport, repeated on every line, in place of the
    content."""
    await _seed(maker, _CAPTURE)

    detail = await StationsReader(maker).station(OWNER, "N1MPR-C", window="1d")

    assert detail is not None
    assert detail["packets"][0]["text"].startswith("!2835.06ND")
    assert detail["gated"] is True and detail["relay"] == "N4TDX"
    # All-time, not the window: it is what says whether an empty window means quiet or
    # means new.
    assert detail["packets_total"] == 2
    assert detail["window_packets"]["1d"] == 1


async def test_a_station_detail_counts_only_its_own_traffic(
    maker: async_sessionmaker, clean: None
) -> None:
    # The time tabs inside a station must be about that station. Showing the whole
    # band's counts there is a control answering a question the owner did not ask.
    await _seed(maker, _CAPTURE)

    detail = await StationsReader(maker).station(OWNER, "N1KSC-1", window="1d")

    assert detail is not None
    assert detail["window_packets"] == {"1d": 2, "3d": 2, "1w": 2, "old": 0}
    assert detail["kind_packets"] == {"Position": 1, "Other": 1}
    assert len(detail["packets"]) == 2
    only_other = await StationsReader(maker).station(OWNER, "N1KSC-1", window="1d", kinds=["Other"])
    assert only_other is not None
    assert [p["kind"] for p in only_other["packets"]] == ["Other"]
    # The chip counts still describe the window, not the selection.
    assert only_other["kind_packets"] == detail["kind_packets"]


async def test_a_station_never_heard_from_is_absent_rather_than_empty(
    maker: async_sessionmaker, clean: None
) -> None:
    # The route turns this into a 404. An empty detail page for a callsign the box has
    # never decoded reads as "heard, said nothing", which is a different fact.
    await _seed(maker, _CAPTURE)

    assert await StationsReader(maker).station(OWNER, "W1AW") is None


async def test_a_non_owner_sees_no_stations(maker: async_sessionmaker, clean: None) -> None:
    """RLS decides, not the reader.

    The roster is a list of who has been transmitting near the owner's house, built from
    third parties' traffic. It is owner-only for the same reason the packets are."""
    await _seed(maker, _CAPTURE)

    roster = await StationsReader(maker).roster(UNSCOPED, window="1d")

    assert roster["stations"] == []
    assert roster["stations_total"] == 0
    assert await StationsReader(maker).station(UNSCOPED, "N1MPR-C") is None
