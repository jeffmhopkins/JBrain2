"""Pure unit tests for the device-silence judgment (no DB, fixed clock)."""

from datetime import UTC, datetime, timedelta

from jbrain.locations import devicehealth

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def test_fix_age_is_seconds_since_last_fix() -> None:
    assert devicehealth.fix_age_seconds(NOW - timedelta(minutes=10), NOW) == 600.0


def test_fix_age_clamps_future_skew_to_zero() -> None:
    # A fix stamped slightly ahead of `now` reads as just-now, never negative.
    assert devicehealth.fix_age_seconds(NOW + timedelta(seconds=5), NOW) == 0.0


def test_never_reported_has_no_age_and_is_not_silent() -> None:
    assert devicehealth.fix_age_seconds(None, NOW) is None
    assert devicehealth.is_silent(None, NOW, revoked=False) is False


def test_fresh_device_is_not_silent() -> None:
    assert devicehealth.is_silent(NOW - timedelta(minutes=20), NOW, revoked=False) is False


def test_dark_active_device_is_silent() -> None:
    assert devicehealth.is_silent(NOW - timedelta(hours=3), NOW, revoked=False) is True


def test_revoked_device_is_never_silent() -> None:
    # Its quiet is expected, not a failure — the `revoked` flag carries that meaning.
    assert devicehealth.is_silent(NOW - timedelta(days=5), NOW, revoked=True) is False


def test_horizon_is_the_exclusive_boundary() -> None:
    horizon = devicehealth.SILENT_AFTER_SECONDS
    at = NOW - timedelta(seconds=horizon)
    just_past = NOW - timedelta(seconds=horizon + 1)
    assert devicehealth.is_silent(at, NOW, revoked=False) is False
    assert devicehealth.is_silent(just_past, NOW, revoked=False) is True
