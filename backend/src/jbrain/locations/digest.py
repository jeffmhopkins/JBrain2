"""The nightly/weekly place digest — a COMPUTE-ON-READ rollup (L7a).

The red-team resolved the original "scheduled compile" framing: `runs`/`run_steps`
store only enqueue metadata, so a scheduled action would have nowhere to write its
output. There is therefore NO new table, NO migration, NO scheduler — the digest is
computed at request time from the owner's own device dwells (`app.events` geofence
transitions, paired into stays by `SqlLocationRepo.dwells`).

Two of the tables behind this — `app.events` and `place_geofence` — are WEAK RLS
(`has_domain_scope` only, which a narrowed owner still satisfies), so the digest
MUST run under a *full owner* context. The repo's `dwells`/`places` already gate on
`require_full_owner`, and the endpoint gates again — RLS will not fail these closed.

Names + times only. A coordinate never enters a digest: the compute reads paired
dwells (place name + interval) and place names, never a fix coordinate.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jbrain.locations import Dwell

_UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class PlaceSegment:
    """One stay on a day's place-track: a named place and the fraction of the local
    day [0,1] it spans (start + width). Names + times only — the UI draws the bar
    from `start`/`width`, never a coordinate. `place_name` None marks a 'no signal'
    gap (no transition data for that span)."""

    place_name: str | None
    start: float
    width: float
    entered_at: datetime
    exited_at: datetime


@dataclass(frozen=True)
class DayTrack:
    """One local civil day as a horizontal place-track: the day's date, its ordered
    segments (named stays + gaps), whether the owner was home for any part of it, and
    whether the day carried any signal at all (an all-gap day is 'no data')."""

    day: date
    segments: list[PlaceSegment]
    home: bool
    has_data: bool


@dataclass(frozen=True)
class PlaceSeen:
    """First/last time a place was seen across the digest window (names + times)."""

    place_name: str
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class Trip:
    """The longest single away-from-home stay in the window — the 'longest trip'
    headline. Names + times only (the day it fell on + the place + duration)."""

    place_name: str
    day: date
    entered_at: datetime
    exited_at: datetime
    seconds: float


@dataclass(frozen=True)
class Digest:
    """The computed place digest for a period: per-day tracks plus the headline
    rollups (nights home, places visited, longest trip, first/last-seen). Coordinate-
    free by construction — every field is a name, a time, a date, or a count."""

    period: str  # 'week' | 'night'
    since: datetime
    until: datetime
    timezone: str
    days: list[DayTrack]
    nights_home: int
    nights_total: int
    places_visited: int
    longest_trip: Trip | None
    seen: list[PlaceSeen] = field(default_factory=list)
    computed_at: datetime | None = None


def _zone(tz: str | None) -> ZoneInfo:
    """The owner's zone, defaulting to UTC when unknown — civil-date math never
    raises mid-digest (it degrades to UTC days, the same fallback the tools use)."""
    if tz is None:
        return _UTC
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return _UTC


def _local_date(dt: datetime, tz: ZoneInfo) -> date:
    """An instant's LOCAL civil date. DST-safe by construction: `astimezone` applies
    the offset in effect at `dt`, so a date never shifts because a flat 24h was
    assumed across a transition (mirrors `locationtools._localize_date`)."""
    return dt.astimezone(tz).date()


def _is_home(place_name: str | None, home_name: str | None) -> bool:
    """Whether a dwell's place is the home anchor (case-insensitive name match). The
    home place is resolved by name, exactly as the dwell tools resolve nights-away;
    None home_name (no home saved) means nothing counts as home."""
    if place_name is None or home_name is None:
        return False
    return place_name.casefold() == home_name.casefold()


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The [midnight, next-midnight) UTC instants bounding a local civil day. Built
    from local midnights so the span is the true civil day length (23/24/25h across a
    DST transition), never a flat 24h."""
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def _gap(start: float, finish: float, start_utc: datetime, span: float) -> PlaceSegment:
    """A 'no signal' segment spanning [start, finish) of the day (place_name None)."""
    return PlaceSegment(
        place_name=None,
        start=start,
        width=finish - start,
        entered_at=start_utc + timedelta(seconds=start * span),
        exited_at=start_utc + timedelta(seconds=finish * span),
    )


def _segments_for_day(
    dwells: list[Dwell], day: date, tz: ZoneInfo
) -> tuple[list[PlaceSegment], bool]:
    """The ordered place-segments overlapping one local civil day, each clamped to
    the day's bounds and expressed as a fraction [0,1] of the day. Returns the
    segments and whether the day carried any dwell at all. Gaps between consecutive
    stays (and before the first / after the last) are emitted as `place_name=None`
    'no signal' segments so the track reads continuously across the whole day."""
    start_utc, end_utc = _day_bounds(day, tz)
    span = (end_utc - start_utc).total_seconds()
    if span <= 0:  # defensive — a real civil day is never zero-length
        return [], False
    clipped: list[tuple[float, float, str, datetime, datetime]] = []
    for d in dwells:
        s = max(d.entered_at, start_utc)
        e = min(d.exited_at, end_utc)
        if e <= s:
            continue
        clipped.append(
            (
                (s - start_utc).total_seconds() / span,
                (e - start_utc).total_seconds() / span,
                d.place_name,
                s,
                e,
            )
        )
    clipped.sort(key=lambda c: c[0])
    has_data = bool(clipped)
    segments: list[PlaceSegment] = []
    cursor = 0.0
    for start, finish, name, s, e in clipped:
        if start > cursor + 1e-6:  # a gap before this stay
            segments.append(_gap(cursor, start, start_utc, span))
        segments.append(PlaceSegment(name, start, max(0.0, finish - start), s, e))
        cursor = max(cursor, finish)
    if cursor < 1.0 - 1e-6:  # a trailing gap to the day's end
        segments.append(_gap(cursor, 1.0, start_utc, span))
    return segments, has_data


def compute_digest(
    dwells: list[Dwell],
    *,
    since: datetime,
    until: datetime,
    timezone: str | None,
    home_name: str | None,
    period: str,
    computed_at: datetime,
) -> Digest:
    """Fold paired dwells into a per-day place digest over `[since, until)`.

    PURE compute (no I/O) so it is unit-testable in isolation: the caller fetches the
    owner's dwells (full-owner gated) and the home place name, and hands them here.

    - Per-day tracks: each LOCAL civil date in the window, its clamped place-segments
      (named stays + 'no signal' gaps), whether it was a home day, and whether it
      carried any data.
    - Nights home: civil dates with ANY home presence; nights total is the window's
      civil-date count, so away = total - home.
    - Places visited: distinct non-home place names seen.
    - Longest trip: the single longest away-from-home stay (by overlap inside the
      window) — names + times only.
    - First/last-seen: per distinct place name across the window.

    Empty `dwells` (no data) yields a graceful digest — every day a no-data gap, zero
    counts, no trip — never an error."""
    tz = _zone(timezone)
    # Clamp each dwell to the window once so every rollup measures time-in-window.
    bounded: list[Dwell] = []
    for d in dwells:
        s = max(d.entered_at, since)
        e = min(d.exited_at, until)
        if e > s:
            bounded.append(Dwell(d.place_entity_id, d.place_name, s, e, (e - s).total_seconds()))

    first_day = _local_date(since, tz)
    last_day = _local_date(until - timedelta(microseconds=1), tz)
    days: list[DayTrack] = []
    home_dates: set[date] = set()
    cursor = first_day
    while cursor <= last_day:
        segments, has_data = _segments_for_day(bounded, cursor, tz)
        home = any(_is_home(seg.place_name, home_name) for seg in segments)
        if home:
            home_dates.add(cursor)
        days.append(DayTrack(day=cursor, segments=segments, home=home, has_data=has_data))
        cursor += timedelta(days=1)

    nights_total = (last_day - first_day).days + 1
    nights_home = len(home_dates)

    seen: dict[str, PlaceSeen] = {}
    visited: set[str] = set()
    longest: Trip | None = None
    for d in bounded:
        if d.place_name is None:
            continue
        if not _is_home(d.place_name, home_name):
            visited.add(d.place_name.casefold())
            if longest is None or d.seconds > longest.seconds:
                longest = Trip(
                    place_name=d.place_name,
                    day=_local_date(d.entered_at, tz),
                    entered_at=d.entered_at,
                    exited_at=d.exited_at,
                    seconds=d.seconds,
                )
        prior = seen.get(d.place_name)
        if prior is None:
            seen[d.place_name] = PlaceSeen(d.place_name, d.entered_at, d.exited_at)
        else:
            seen[d.place_name] = PlaceSeen(
                d.place_name,
                min(prior.first_seen, d.entered_at),
                max(prior.last_seen, d.exited_at),
            )

    return Digest(
        period=period,
        since=since,
        until=until,
        timezone=timezone or "UTC",
        days=days,
        nights_home=nights_home,
        nights_total=nights_total,
        places_visited=len(visited),
        longest_trip=longest,
        seen=sorted(seen.values(), key=lambda p: p.first_seen),
        computed_at=computed_at,
    )
