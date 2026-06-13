"""The RFC 5545 ICS serializer: structure, UTC stamps, the status map, escaping,
folding, recurrence, and all-day events."""

from datetime import UTC, datetime

from jbrain.appointments.ics import to_ics
from jbrain.appointments.service import AppointmentInfo

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def appt(**over) -> AppointmentInfo:
    base = dict(
        id="A1",
        domain="general",
        entity_id="e1",
        title="Dentist",
        starts_at=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
        ends_at=None,
        all_day=False,
        location=None,
        status="confirmed",
        rrule=None,
        attendees=[],
        source_note_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    base.update(over)
    return AppointmentInfo(**base)  # type: ignore[arg-type]


def lines(text: str) -> list[str]:
    # CRLF endings per RFC 5545; unfold continuations for content assertions.
    return text.replace("\r\n ", "").rstrip("\r\n").split("\r\n")


def test_where_who_facets_emit_their_ical_properties() -> None:
    body = lines(
        to_ics(
            [
                appt(
                    organizer="Maple Dental",
                    description="Bring x-rays",
                    online_url="https://meet.example/abc",
                    appointment_type="checkup",
                    attendees=[
                        {"name": "Dr. Nguyen", "role": "chair", "status": "accepted"},
                        {"name": "Pat", "required": False},
                        {"name": ""},  # no name → no ATTENDEE line
                    ],
                )
            ],
            now=NOW,
        )
    )
    nomail = "mailto:noreply@jbrain.invalid"
    assert "DESCRIPTION:Bring x-rays" in body
    assert f"ORGANIZER;CN=Maple Dental:{nomail}" in body
    assert f"ATTENDEE;CN=Dr. Nguyen;ROLE=CHAIR;PARTSTAT=ACCEPTED:{nomail}" in body
    assert f"ATTENDEE;CN=Pat;ROLE=OPT-PARTICIPANT:{nomail}" in body
    assert sum(line.startswith("ATTENDEE") for line in body) == 2  # blank dropped
    assert "CONFERENCE;VALUE=URI;FEATURE=VIDEO;LABEL=Join:https://meet.example/abc" in body
    assert "CATEGORIES:checkup" in body


def test_a_bare_appointment_omits_the_optional_properties() -> None:
    body = lines(to_ics([appt()], now=NOW))
    assert not any(
        line.startswith(("DESCRIPTION", "ORGANIZER", "ATTENDEE", "CONFERENCE", "CATEGORIES"))
        for line in body
    )


def test_empty_calendar_is_well_formed() -> None:
    out = to_ics([], now=NOW)
    assert out.endswith("\r\n")
    body = lines(out)
    assert body[0] == "BEGIN:VCALENDAR"
    assert "VERSION:2.0" in body
    assert "PRODID:-//JBrain//Appointments//EN" in body
    assert body[-1] == "END:VCALENDAR"
    assert "BEGIN:VEVENT" not in body


def test_timed_event_has_utc_stamps_summary_and_status() -> None:
    body = lines(to_ics([appt(ends_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC))], now=NOW))
    assert "BEGIN:VEVENT" in body
    assert "UID:A1@jbrain" in body
    assert "DTSTAMP:20260601T120000Z" in body
    assert "DTSTART:20260701T140000Z" in body
    assert "DTEND:20260701T150000Z" in body
    assert "SUMMARY:Dentist" in body
    assert "STATUS:CONFIRMED" in body


def test_status_map_and_recurrence() -> None:
    body = lines(to_ics([appt(status="cancelled", rrule="FREQ=WEEKLY;BYDAY=MO")], now=NOW))
    assert "STATUS:CANCELLED" in body
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in body
    # `tentative` maps through; `occurred` has no ICS equivalent → CONFIRMED.
    assert "STATUS:TENTATIVE" in lines(to_ics([appt(status="tentative")], now=NOW))
    assert "STATUS:CONFIRMED" in lines(to_ics([appt(status="occurred")], now=NOW))


def test_rrule_control_chars_are_stripped() -> None:
    # A malformed recurrence token must never break the iCalendar line structure.
    out = to_ics([appt(rrule="FREQ=WEEKLY\r\nX-EVIL:1")], now=NOW)
    assert "RRULE:FREQ=WEEKLYX-EVIL:1" in lines(out)
    # No stray VEVENT-internal newline injected an extra physical line.
    assert "X-EVIL:1" not in out.split("\r\n")


def test_all_day_uses_date_values() -> None:
    body = lines(
        to_ics([appt(all_day=True, starts_at=datetime(2026, 7, 4, 0, 0, tzinfo=UTC))], now=NOW)
    )
    assert "DTSTART;VALUE=DATE:20260704" in body
    assert not any(line.startswith("DTSTART:") for line in body)
    # A multi-day all-day event carries a DATE-valued DTEND too.
    multi = lines(
        to_ics(
            [
                appt(
                    all_day=True,
                    starts_at=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
                    ends_at=datetime(2026, 7, 6, 0, 0, tzinfo=UTC),
                )
            ],
            now=NOW,
        )
    )
    assert "DTEND;VALUE=DATE:20260706" in multi


def test_text_values_are_escaped() -> None:
    body = lines(to_ics([appt(title="Lunch; w/ A, B\nback room", location="Café, 2nd")], now=NOW))
    assert "SUMMARY:Lunch\\; w/ A\\, B\\nback room" in body
    assert "LOCATION:Café\\, 2nd" in body


def test_long_summary_is_folded_at_75_octets() -> None:
    out = to_ics([appt(title="x" * 200)], now=NOW)
    # Every physical line stays within the 75-octet limit (CRLF excluded).
    for physical in out.split("\r\n"):
        assert len(physical.encode("utf-8")) <= 75
    # And unfolding restores the full summary.
    assert ("SUMMARY:" + "x" * 200) in lines(out)
