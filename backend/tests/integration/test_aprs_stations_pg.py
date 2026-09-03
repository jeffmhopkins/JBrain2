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
from jbrain.sdr.stations import MAX_STATIONS, StationsReader
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


async def test_a_station_matching_a_chip_is_returned_WHOLE(
    maker: async_sessionmaker, clean: None
) -> None:
    """The wave's headline claim, tested where a `WHERE` would look identical.

    Selecting Weather returns the station AND ALL ITS TRAFFIC — not its weather frames
    with its positions stripped out. Every station in the main capture happens to match
    either way; this one does not, so swapping the `HAVING` for a `WHERE` changes the
    answer."""
    # N1KSC-1 sends a position AND two telemetry frames. That MIX is what separates the
    # two readings: a `WHERE` would return the station with only its matching frame.
    await _seed(
        maker,
        [
            ("N1KSC-1", DIRECT_POSITION, 0.1),
            ("N1KSC-1", DIRECT_TELEMETRY, 0.2),
            ("N1KSC-1", DIRECT_TELEMETRY, 0.3),
            ("N4TDX", GATED_WEATHER, 0.4),
        ],
    )

    position = await StationsReader(maker).roster(OWNER, window="1d", kinds=["Position"])

    # Three packets, not one: "who is putting out positions" returns the station, and a
    # station is all of its traffic. KD4WLE sends no position at all, so it is absent.
    assert {s["call"]: s["packets"] for s in position["stations"]} == {"N1KSC-1": 3}
    # The subtitle still names everything it sends, not just what was selected.
    assert position["stations"][0]["kinds"] == ["Other", "Position"]


async def test_a_station_reports_how_it_reached_us_MOST_RECENTLY(
    maker: async_sessionmaker, clean: None
) -> None:
    """How a station arrives can change between packets, and the line is about now.

    A station gated from the internet this morning and heard on the air this afternoon
    reads "heard on RF"; reading its oldest frame instead would tell the owner a station
    in range had never been on the air."""
    now = datetime.now(UTC)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            _INSERT,
            {"heard_at": now - timedelta(hours=5), "src": "N4TDX", "info": GATED_POSITION},
        )
        await s.execute(
            _INSERT,
            {"heard_at": now - timedelta(minutes=6), "src": "N1MPR-C", "info": DIRECT_POSITION},
        )
        await s.commit()
    await AprsLog(maker=maker, base_url="").backfill()

    (station,) = (await StationsReader(maker).roster(OWNER, window="1d"))["stations"]

    assert station["call"] == "N1MPR-C"
    assert (station["gated"], station["relay"]) == (False, None)


async def test_a_station_lists_the_kinds_it_actually_sends(
    maker: async_sessionmaker, clean: None
) -> None:
    # The roster subtitle. Sorted rather than in insertion order, so the line does not
    # reshuffle itself between polls.
    await _seed(maker, _CAPTURE)

    by_call = {s["call"]: s for s in (await StationsReader(maker).roster(OWNER))["stations"]}

    assert by_call["N1KSC-1"]["kinds"] == ["Other", "Position"]
    assert by_call["KD4WLE"]["kinds"] == ["Weather"]


async def test_the_roster_is_capped_and_says_when_it_capped(
    maker: async_sessionmaker, clean: None
) -> None:
    """A capped list that does not say so is a list that hides a station.

    The cap is real — an archive can hold more stations than a phone can render — so
    what matters is that the header can tell the truth about it."""
    await _seed(maker, _CAPTURE)
    reader = StationsReader(maker)

    two = await reader.roster(OWNER, window="1d", limit=2)

    assert len(two["stations"]) == 2
    assert two["truncated"] is True
    assert two["stations_total"] == 3  # the honest total survives the cap
    assert (await reader.roster(OWNER, window="1d"))["truncated"] is False
    # And a nonsense limit is clamped rather than trusted.
    assert len((await reader.roster(OWNER, limit=0))["stations"]) == 1
    assert len((await reader.roster(OWNER, limit=10_000))["stations"]) == 3


async def test_a_caller_cannot_ask_for_more_stations_than_the_ceiling(
    maker: async_sessionmaker, clean: None
) -> None:
    """The ceiling is the ceiling, whatever the query string says.

    A limit that is merely a default is not a bound — an unclamped one lets a request
    ask the box to render every station it has ever heard, which on a year of this
    channel is the whole archive grouped in one statement."""
    now = datetime.now(UTC)
    async with scoped_session(maker, OWNER) as s:
        # Pre-classified, so this is a cheap seed rather than a sweep over 350 frames.
        await s.execute(
            text(
                "INSERT INTO app.aprs_packets (heard_at, frequency_hz, source, destination,"
                " path, info, raw, origin_call, kind, gated, heard_direct)"
                " SELECT (:now)::timestamptz - (g || ' seconds')::interval, 144390000,"
                " 'W4' || g, 'APRS', '{}', :info, '', 'W4' || g, 'Position', false, true"
                " FROM generate_series(1, 350) g"
            ),
            {"now": now, "info": DIRECT_POSITION},
        )
        await s.commit()

    asked = await StationsReader(maker).roster(OWNER, window="1d", limit=100_000)

    assert len(asked["stations"]) == MAX_STATIONS
    assert asked["truncated"] is True
    assert asked["stations_total"] == 350


async def test_the_owner_is_pinned_before_the_list_is_capped(
    maker: async_sessionmaker, clean: None
) -> None:
    """The one station this feature exists to surface, and the cap can eat it.

    Pinning only in the client cannot pin what the client never received: over a year's
    archive the owner's station falls outside the most-recently-heard cap, and the
    screen then shows every station except his."""
    await _seed(maker, _CAPTURE)
    reader = StationsReader(maker)

    # KD4WLE is the oldest-heard station in the window, so a cap of one drops it.
    unpinned = await reader.roster(OWNER, window="1d", limit=1)
    assert [s["call"] for s in unpinned["stations"]] == ["N1MPR-C"]

    pinned = await reader.roster(OWNER, window="1d", limit=1, mine="KD4WLE")

    assert [s["call"] for s in pinned["stations"]] == ["KD4WLE"]
    # A bare callsign means every SSID of it, and NOTHING that merely starts with it —
    # `N1` is not a station and must not pin `N1KSC-1`.
    by_prefix = await reader.roster(OWNER, window="1d", limit=1, mine="N1")
    assert [s["call"] for s in by_prefix["stations"]] == ["N1MPR-C"]  # unpinned order


async def test_the_windows_nest_and_older_is_the_complement(
    maker: async_sessionmaker, clean: None
) -> None:
    # "3 days" contains "1 day"; "Older" is the one exclusive bucket, and the only one
    # that can be empty while the others are full.
    await _seed(maker, _CAPTURE)

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    assert roster["window_packets"] == {"1d": 4, "3d": 5, "1w": 5}
    # `old` is PRESENCE here, not a count: counting it means reading everything the box
    # has ever heard, on every poll, for a number nobody reads precisely.
    assert (roster["has_older"], roster["older"]) == (True, None)

    older = await StationsReader(maker).roster(OWNER, window="old")

    assert [s["call"] for s in older["stations"]] == ["N1MPR-C"]
    # Opening that range is when the exact count is worth the read.
    assert older["older"] == 1


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
    # `direct` is the fact F1's review had to fix: a relayed frame is never the sender's
    # own transmission as we received it, however clean its inner path.
    assert detail["packets"][0]["direct"] is False
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
    assert detail["window_packets"] == {"1d": 2, "3d": 2, "1w": 2}
    # Inside a station the archive count IS cheap — the origin index bounds it to one
    # station's rows — so it is always exact.
    assert (detail["has_older"], detail["older"]) == (False, 0)
    assert [p["direct"] for p in detail["packets"]] == [True, True]
    assert detail["kind_packets"] == {"Position": 1, "Other": 1}
    assert len(detail["packets"]) == 2
    only_other = await StationsReader(maker).station(OWNER, "N1KSC-1", window="1d", kinds=["Other"])
    assert only_other is not None
    # The chip filters on the stored BUCKET; the row's title says what it actually is.
    # A chip row of five is a control you can aim, and "Other" as a title tells a reader
    # nothing — so telemetry lives in the Other bucket and titles itself Telemetry.
    assert [p["bucket"] for p in only_other["packets"]] == ["Other"]
    assert [p["kind"] for p in only_other["packets"]] == ["Telemetry"]
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


async def test_a_packet_arrives_decoded_with_its_frame_and_its_id(
    maker: async_sessionmaker, clean: None
) -> None:
    """The row is a sentence, and the evidence for it is one tap below.

    Both halves are checked here because both were previously thrown away: `stations.py`
    already SELECTed source, path and raw on every poll and discarded them, and the row
    id — which is what stops the client keying on an array index — was never sent."""
    await _seed(maker, [("N4TDX", GATED_WEATHER, 0.1)])

    detail = await StationsReader(maker).station(OWNER, "KD4WLE", window="1d")

    assert detail is not None
    (packet,) = detail["packets"]
    assert packet["id"]  # a stable identity, not a position in a list
    assert packet["kind"] == "Weather"
    assert "82 °F" in packet["summary"]
    assert packet["symbol"] == "/_"
    values = dict(packet["fields"])
    assert values["Wind"] == "from the NW (317°) at 2 mph"
    # Hundredths of an inch. The raw `p063` means nothing as read.
    assert values["Rain, last 24 hours"] == "0.63 in"
    # The frame as heard — the only place "gated via N4TDX" becomes checkable, because
    # the row itself deliberately shows the inner payload rather than the wrapper.
    assert packet["frame"]["source"] == "N4TDX"


async def test_a_station_s_own_telemetry_definitions_are_applied(
    maker: async_sessionmaker, clean: None
) -> None:
    """Five raw numbers become volts and packet counts — but only because this station
    published what its channels measure, in ordinary messages sitting in the same table.

    The definitions are read per station, not per packet, and only self-definitions
    count: anyone with a transmitter can send `:N1KSC-1 :EQNS.0,1000000,0`."""
    await _seed(
        maker,
        [
            ("N1KSC-1", ":N1KSC-1  :PARM.Vin,Rx1h,Dg1h,Eff1h,A5", 0.4),
            ("N1KSC-1", ":N1KSC-1  :UNIT.Volt,Pkt,Pkt,Pcnt,None", 0.3),
            ("N1KSC-1", ":N1KSC-1  :EQNS.0,0.075,0,0,10,0,0,10,0,0,1,0,0,0,0", 0.2),
            ("N1KSC-1", "T#110,190,088,011,068,000,00000000", 0.1),
        ],
    )

    detail = await StationsReader(maker).station(OWNER, "N1KSC-1", window="1d")

    assert detail is not None
    telemetry = next(p for p in detail["packets"] if p["kind"] == "Telemetry")
    values = dict(telemetry["fields"])
    assert values["Vin"] == "14.25 Volt"
    assert values["Dg1h"] == "110 Pkt"
    # And the row itself reads as a sentence rather than as five numbers.
    assert telemetry["summary"].startswith("Vin 14.25 Volt")


async def test_a_station_that_never_said_what_it_measures_shows_raw_numbers(
    maker: async_sessionmaker, clean: None
) -> None:
    # The honest answer. Inventing units for an undeclared channel would put a confident
    # wrong reading on screen with nothing to contradict it.
    await _seed(maker, [("K4KSC-12", "T#353,053,001,077,000,016,11000000", 0.1)])

    detail = await StationsReader(maker).station(OWNER, "K4KSC-12", window="1d")

    assert detail is not None
    (packet,) = detail["packets"]
    assert dict(packet["fields"])["Channel 1"] == "053"
    assert any("has not published" in w for w in packet["warnings"])


async def test_the_roster_says_what_each_station_last_sent(
    maker: async_sessionmaker, clean: None
) -> None:
    """The roster answered who and how many but never WHAT.

    A LATERAL per row rather than another `array_agg(...)[1]`, so this is the test that a
    join returning the WRONG station's frame would fail — the failure mode that matters,
    because a plausible reading under the wrong callsign is invisible on screen."""
    await _seed(
        maker,
        [
            ("N4TDX", GATED_WEATHER, 0.3),
            ("K4KSC-1", DIRECT_POSITION, 0.2),
            ("N4TDX", GATED_WEATHER, 0.1),
        ],
    )

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    by_call = {s["call"]: s for s in roster["stations"]}
    assert "82 °F" in by_call["KD4WLE"]["last_summary"]
    assert by_call["KD4WLE"]["last_kind"] == "Weather"
    assert by_call["KD4WLE"]["last_symbol"] == "/_"
    # The OTHER station's newest frame, not the busiest one's.
    assert by_call["K4KSC-1"]["last_kind"] == "Position"
    assert by_call["K4KSC-1"]["last_symbol"] != "/_"


async def test_the_roster_row_carries_the_NEWEST_frame_not_the_first(
    maker: async_sessionmaker, clean: None
) -> None:
    # "Last heard" and "what it last said" have to name the same packet, or the row's
    # time and its reading describe two different moments.
    await _seed(
        maker,
        [
            ("WX", "@031030z2837.27N/08049.42W_338/000g000t070", 0.4),
            ("WX", "@031430z2837.27N/08049.42W_338/000g000t088", 0.1),
        ],
    )

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    (station,) = [s for s in roster["stations"] if s["call"] == "WX"]
    assert "88 °F" in station["last_summary"]
    assert "70 °F" not in station["last_summary"]


async def test_a_station_with_one_unreadable_frame_still_lists(
    maker: async_sessionmaker, clean: None
) -> None:
    # The decode is per row on READ, and `explain` is total — but a roster that raised on
    # one bad frame would lose the whole list, not one line.
    await _seed(maker, [("ODD", "\x01\x02 not a frame", 0.1)])

    roster = await StationsReader(maker).roster(OWNER, window="1d")

    assert any(s["call"] == "ODD" for s in roster["stations"])
