"""Computing a task's next fire time from its schedule spec — a pure function so it
is fully testable with a frozen clock (mirrors the workflow scheduler's app-side
`next_run_at` advance, docs/archive/WORKFLOW_ENGINE_PLAN.md).

Weekday convention is Sunday=0 … Saturday=6, matching the editor's `S M T W T F S`
chip row; Python's `date.weekday()` (Monday=0 … Sunday=6) is converted on the way
in. `repeat` times are interpreted in the task's IANA timezone so "every weekday at
7:00" means 7:00 *local*, then stored in UTC.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Kinds and frequencies — the closed sets the API and DB CHECKs share.
KINDS = frozenset({"on_demand", "once", "repeat"})
FREQS = frozenset({"daily", "weekdays", "weekly"})

# Monday..Friday in the Sunday=0 convention.
_WEEKDAYS = frozenset({1, 2, 3, 4, 5})
# How far ahead to search for the next matching day (a week + today's wrap).
_SEARCH_DAYS = 8


@dataclass(frozen=True)
class ScheduleSpec:
    kind: str
    freq: str | None = None
    days: tuple[int, ...] = ()
    time: str | None = None
    run_at: datetime | None = None
    tz: str = "UTC"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _sun0(d: datetime) -> int:
    """Python weekday (Mon=0..Sun=6) → Sunday=0..Saturday=6."""
    return (d.weekday() + 1) % 7


def _allowed_days(spec: ScheduleSpec) -> set[int]:
    if spec.freq == "daily":
        return set(range(7))
    if spec.freq == "weekdays":
        return set(_WEEKDAYS)
    if spec.freq == "weekly":
        return {d for d in spec.days if 0 <= d <= 6}
    return set()


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        hh, mm = value.split(":", 1)
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def next_run_after(spec: ScheduleSpec, after: datetime) -> datetime | None:
    """The next fire instant strictly after `after` (an aware UTC datetime), or None
    when the task never auto-fires again: an on-demand task, a one-off whose moment
    has passed, or a malformed repeat spec.

    `after` is treated as UTC if naive (defensive — callers pass aware UTC)."""
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    after = after.astimezone(UTC)

    if spec.kind == "once":
        if spec.run_at is None:
            return None
        run_at = spec.run_at if spec.run_at.tzinfo else spec.run_at.replace(tzinfo=UTC)
        run_at = run_at.astimezone(UTC)
        return run_at if run_at > after else None

    if spec.kind != "repeat":
        return None

    hhmm = _parse_hhmm(spec.time)
    days = _allowed_days(spec)
    if hhmm is None or not days:
        return None
    h, m = hhmm
    tz = _zone(spec.tz)
    after_local = after.astimezone(tz)
    for offset in range(_SEARCH_DAYS):
        cand_date = (after_local + timedelta(days=offset)).date()
        cand_local = datetime.combine(cand_date, time(h, m), tzinfo=tz)
        if _sun0(cand_local) not in days:
            continue
        cand_utc = cand_local.astimezone(UTC)
        if cand_utc > after:
            return cand_utc
    return None


def spec_from(
    *,
    kind: str,
    freq: str | None,
    days: Sequence[int],
    time: str | None,
    run_at: datetime | None,
    tz: str,
) -> ScheduleSpec:
    return ScheduleSpec(
        kind=kind, freq=freq, days=tuple(days), time=time, run_at=run_at, tz=tz or "UTC"
    )
