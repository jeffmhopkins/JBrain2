"""Unit tests for the appointment-projection helpers — the pure value coercions
that turn a fact's value_json into calendar fields (no DB)."""

from datetime import UTC, datetime

from jbrain.analysis.appointment_projection import _coerce_status, _parse_dt


def test_parse_dt_accepts_datetime_iso_and_rejects_junk() -> None:
    when = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    assert _parse_dt(when) is when
    assert _parse_dt("2026-07-01T09:00:00+00:00") == when
    assert _parse_dt("not a date") is None
    assert _parse_dt(None) is None
    assert _parse_dt(42) is None


def test_coerce_status_reads_enum_members_only() -> None:
    # Bare string and the common dict shapes, case-insensitively.
    assert _coerce_status("cancelled") == "cancelled"
    assert _coerce_status("Confirmed") == "confirmed"
    assert _coerce_status({"value": "tentative"}) == "tentative"
    assert _coerce_status({"status": "occurred"}) == "occurred"
    # Anything off the appointment.yaml enum is None (the default applies).
    assert _coerce_status("maybe") is None
    assert _coerce_status({"value": "maybe"}) is None
    assert _coerce_status({"other": "confirmed"}) is None
    assert _coerce_status(None) is None
