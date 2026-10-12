"""Unit tests for the appointment-projection helpers — the pure value coercions
that turn a fact's value_json into calendar fields (no DB)."""

from datetime import UTC, datetime

from jbrain.analysis.appointment_projection import (
    _ONLINE_URL,
    _coerce_status,
    _parse_dt,
    _row_facets,
)
from jbrain.models.analysis import Fact


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


def test_online_url_keeps_web_links_and_drops_script_schemes() -> None:
    # The join link is email/LLM-extracted and rendered as an href in the PWA, so only an
    # absolute http(s) URL survives projection; anything else would run in the app origin.
    def project(url: object) -> object:
        return _row_facets({_ONLINE_URL: [Fact(value_json=url)]}, {})["online_url"]

    assert project("https://meet.example.com/abc?pwd=1") == "https://meet.example.com/abc?pwd=1"
    assert project({"url": "http://zoom.example/j/1"}) == "http://zoom.example/j/1"
    assert project(" HTTPS://Meet.example/x ") == "HTTPS://Meet.example/x"
    for bad in (
        "javascript:alert(document.cookie)",
        "JAVASCRIPT:alert(1)",
        "  javascript:alert(1)",
        {"url": "javascript:alert(1)"},
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "https:no-host",
        "meet.example.com/abc",
    ):
        assert project(bad) is None, bad
