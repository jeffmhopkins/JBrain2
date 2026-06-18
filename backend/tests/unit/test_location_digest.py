"""The place-digest compute (L7a) — pure, no I/O. Proves the per-day buckets,
nights-home, longest trip, first/last-seen, the tz/DST civil-date math, the empty
window, and that no coordinate ever enters the result."""

from datetime import UTC, date, datetime, timedelta

from jbrain.locations import Dwell
from jbrain.locations.digest import compute_digest

NY = "America/New_York"
NOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


def _dwell(place: str, entered: datetime, exited: datetime) -> Dwell:
    return Dwell(
        place_entity_id=f"ent-{place}",
        place_name=place,
        entered_at=entered,
        exited_at=exited,
        seconds=(exited - entered).total_seconds(),
    )


def test_empty_window_is_graceful() -> None:
    since = NOW - timedelta(days=7)
    d = compute_digest(
        [], since=since, until=NOW, timezone=NY, home_name="Home", period="week", computed_at=NOW
    )
    assert d.period == "week"
    assert d.nights_home == 0 and d.places_visited == 0
    assert d.longest_trip is None and d.seen == []
    # Every day is present but carries no data — an all-gap track, never an error.
    assert d.nights_total == len(d.days) >= 7
    assert all(not day.has_data for day in d.days)
    assert all(not day.home for day in d.days)


def test_nights_home_counts_local_dates_with_home_presence() -> None:
    # Home on two distinct local dates within the week; away the rest. Midday-UTC
    # stays so the local date (UTC tz here) is unambiguous — no midnight crossing.
    since = NOW - timedelta(days=7)
    dwells = [
        _dwell(
            "Home", datetime(2026, 6, 16, 1, 0, tzinfo=UTC), datetime(2026, 6, 16, 9, 0, tzinfo=UTC)
        ),
        _dwell(
            "Home", datetime(2026, 6, 17, 1, 0, tzinfo=UTC), datetime(2026, 6, 17, 9, 0, tzinfo=UTC)
        ),
        _dwell(
            "Office",
            datetime(2026, 6, 16, 13, 0, tzinfo=UTC),
            datetime(2026, 6, 16, 21, 0, tzinfo=UTC),
        ),
    ]
    d = compute_digest(
        dwells,
        since=since,
        until=NOW,
        timezone="UTC",
        home_name="Home",
        period="week",
        computed_at=NOW,
    )
    assert d.nights_home == 2
    # Office is the one non-home place visited.
    assert d.places_visited == 1
    # The home days flag home; an office-only day is not home.
    home_days = {day.day for day in d.days if day.home}
    assert date(2026, 6, 16) in home_days and date(2026, 6, 17) in home_days


def test_home_match_is_case_insensitive() -> None:
    since = NOW - timedelta(days=1)
    dwells = [_dwell("home", NOW - timedelta(hours=3), NOW - timedelta(hours=1))]
    d = compute_digest(
        dwells,
        since=since,
        until=NOW,
        timezone="UTC",
        home_name="Home",
        period="night",
        computed_at=NOW,
    )
    assert d.nights_home >= 1 and d.places_visited == 0


def test_longest_trip_is_the_longest_away_stay() -> None:
    since = NOW - timedelta(days=7)
    dwells = [
        _dwell(
            "Office",
            datetime(2026, 6, 15, 13, 0, tzinfo=UTC),
            datetime(2026, 6, 15, 17, 0, tzinfo=UTC),
        ),
        # The long Saturday trip — 10h, the longest non-home stay.
        _dwell(
            "Pearl St",
            datetime(2026, 6, 13, 14, 0, tzinfo=UTC),
            datetime(2026, 6, 14, 0, 0, tzinfo=UTC),
        ),
        _dwell(
            "Home",
            datetime(2026, 6, 16, 2, 0, tzinfo=UTC),
            datetime(2026, 6, 16, 11, 0, tzinfo=UTC),
        ),
    ]
    d = compute_digest(
        dwells,
        since=since,
        until=NOW,
        timezone=NY,
        home_name="Home",
        period="week",
        computed_at=NOW,
    )
    assert d.longest_trip is not None
    assert d.longest_trip.place_name == "Pearl St"
    assert d.longest_trip.seconds == 10 * 3600.0
    # Home is never a "trip".
    assert d.longest_trip.place_name != "Home"


def test_first_and_last_seen_per_place() -> None:
    since = NOW - timedelta(days=7)
    dwells = [
        _dwell(
            "Mom's house",
            datetime(2026, 6, 16, 18, 0, tzinfo=UTC),
            datetime(2026, 6, 16, 20, 0, tzinfo=UTC),
        ),
        _dwell(
            "Mom's house",
            datetime(2026, 6, 17, 18, 0, tzinfo=UTC),
            datetime(2026, 6, 17, 19, 0, tzinfo=UTC),
        ),
    ]
    d = compute_digest(
        dwells,
        since=since,
        until=NOW,
        timezone=NY,
        home_name="Home",
        period="week",
        computed_at=NOW,
    )
    seen = {s.place_name: s for s in d.seen}
    assert "Mom's house" in seen
    assert seen["Mom's house"].first_seen == datetime(2026, 6, 16, 18, 0, tzinfo=UTC)
    assert seen["Mom's house"].last_seen == datetime(2026, 6, 17, 19, 0, tzinfo=UTC)


def test_per_day_segments_clamp_to_the_day_and_fill_gaps() -> None:
    # A single midday stay in UTC: the track has a leading gap, the stay, a trailing
    # gap, all within [0,1] and ordered, totaling the whole day.
    day_start = datetime(2026, 6, 17, 0, 0, tzinfo=UTC)
    dwells = [_dwell("Office", day_start + timedelta(hours=9), day_start + timedelta(hours=17))]
    d = compute_digest(
        dwells,
        since=day_start,
        until=day_start + timedelta(days=1),
        timezone="UTC",
        home_name="Home",
        period="night",
        computed_at=NOW,
    )
    track = next(t for t in d.days if t.day == date(2026, 6, 17))
    assert track.has_data
    # Ordered, in-range, contiguous coverage of the whole day.
    assert track.segments[0].start == 0.0
    cursor = 0.0
    for seg in track.segments:
        assert seg.start >= cursor - 1e-9
        assert 0.0 <= seg.start <= 1.0 and 0.0 <= seg.width <= 1.0
        cursor = seg.start + seg.width
    assert abs(cursor - 1.0) < 1e-6
    # The named stay is ~9/24..17/24, flanked by no-signal gaps.
    named = [s for s in track.segments if s.place_name == "Office"]
    gaps = [s for s in track.segments if s.place_name is None]
    assert len(named) == 1 and len(gaps) == 2
    assert abs(named[0].start - 9 / 24) < 1e-6


def test_dst_spring_forward_keeps_one_day_per_local_date() -> None:
    # US spring-forward 2026-03-08 (a 23h local day). The window spanning it must
    # produce exactly one DayTrack per local civil date — never a skipped/doubled day.
    tz = NY
    since = datetime(2026, 3, 6, 5, 0, tzinfo=UTC)  # ~Mar 6 local
    until = datetime(2026, 3, 11, 5, 0, tzinfo=UTC)  # ~Mar 11 local
    d = compute_digest(
        [], since=since, until=until, timezone=tz, home_name="Home", period="week", computed_at=NOW
    )
    days = [t.day for t in d.days]
    assert days == sorted(days)
    assert len(set(days)) == len(days)  # no repeats
    # The transition date is present exactly once.
    assert days.count(date(2026, 3, 8)) == 1


def test_digest_carries_no_coordinate() -> None:
    since = NOW - timedelta(days=7)
    dwells = [
        _dwell("Office @ 40.0,-74.0", NOW - timedelta(hours=5), NOW - timedelta(hours=1)),
    ]
    d = compute_digest(
        dwells,
        since=since,
        until=NOW,
        timezone=NY,
        home_name="Home",
        period="week",
        computed_at=NOW,
    )
    # Only the (model-given) place name string carries those chars; no numeric lat/lon
    # field exists on any digest dataclass. Assert the structure exposes none.
    blob = repr(d)
    # The place name itself is allowed (it's a name); but there is no separate
    # latitude/longitude/center attribute anywhere.
    assert "latitude" not in blob and "longitude" not in blob and "center" not in blob
