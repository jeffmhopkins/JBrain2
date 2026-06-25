"""The pure next-fire computation (tasks/schedule.py) under a frozen clock."""

from datetime import UTC, datetime

from jbrain.tasks.schedule import ScheduleSpec, next_run_after, spec_from


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_on_demand_never_fires() -> None:
    spec = ScheduleSpec(kind="on_demand")
    assert next_run_after(spec, _utc(2026, 6, 24, 12)) is None


def test_once_future_then_spent() -> None:
    run_at = _utc(2026, 7, 1, 9)
    spec = ScheduleSpec(kind="once", run_at=run_at)
    assert next_run_after(spec, _utc(2026, 6, 24, 12)) == run_at
    # After the moment has passed it never fires again.
    assert next_run_after(spec, _utc(2026, 7, 1, 9)) is None
    assert next_run_after(spec, _utc(2026, 7, 2, 9)) is None


def test_daily_same_day_then_next_day() -> None:
    spec = ScheduleSpec(kind="repeat", freq="daily", time="07:00", tz="UTC")
    # Before 07:00 → today; after → tomorrow.
    assert next_run_after(spec, _utc(2026, 6, 24, 6)) == _utc(2026, 6, 24, 7)
    assert next_run_after(spec, _utc(2026, 6, 24, 8)) == _utc(2026, 6, 25, 7)


def test_weekdays_skips_the_weekend() -> None:
    # 2026-06-26 is a Friday; the next weekday fire is Monday the 29th.
    spec = ScheduleSpec(kind="repeat", freq="weekdays", time="07:00", tz="UTC")
    assert next_run_after(spec, _utc(2026, 6, 26, 8)) == _utc(2026, 6, 29, 7)


def test_weekly_picks_the_chosen_days() -> None:
    # Sun=0, Wed=3; from Mon the next match is Wed.
    spec = ScheduleSpec(kind="repeat", freq="weekly", days=(0, 3), time="09:00", tz="UTC")
    assert next_run_after(spec, _utc(2026, 6, 22, 10)) == _utc(2026, 6, 24, 9)
    # From Wed afternoon the next match wraps to Sunday the 28th.
    assert next_run_after(spec, _utc(2026, 6, 24, 10)) == _utc(2026, 6, 28, 9)


def test_timezone_interprets_local_time() -> None:
    # 07:00 New York on a June day is EDT (UTC-4) → 11:00 UTC.
    spec = ScheduleSpec(kind="repeat", freq="daily", time="07:00", tz="America/New_York")
    assert next_run_after(spec, _utc(2026, 6, 24, 0)) == _utc(2026, 6, 24, 11)


def test_malformed_repeat_never_fires() -> None:
    base = _utc(2026, 6, 24, 0)
    assert next_run_after(ScheduleSpec(kind="repeat", freq="daily", tz="UTC"), base) is None
    assert next_run_after(ScheduleSpec(kind="repeat", time="07:00", tz="UTC"), base) is None
    no_days = ScheduleSpec(kind="repeat", freq="weekly", days=(), time="07:00")
    assert next_run_after(no_days, base) is None


def test_bad_timezone_falls_back_to_utc() -> None:
    spec = ScheduleSpec(kind="repeat", freq="daily", time="07:00", tz="Not/AZone")
    assert next_run_after(spec, _utc(2026, 6, 24, 0)) == _utc(2026, 6, 24, 7)


def test_spec_from_builds_the_spec() -> None:
    spec = spec_from(kind="repeat", freq="weekly", days=[1, 2], time="08:30", run_at=None, tz="UTC")
    assert spec.kind == "repeat" and spec.days == (1, 2) and spec.time == "08:30"
