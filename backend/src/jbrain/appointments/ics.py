"""A hand-rolled RFC 5545 (iCalendar) serializer for the appointments feed — no
runtime dependency. Turns the appointments projection into a VCALENDAR a phone
calendar can subscribe to read-only.

Scope (owner decision): one feed across ALL domains with full titles. The
subscribe URL therefore carries health/finance appointment titles off-box into
whatever calendar subscribes — it is revocable and labelled as such in Settings.
A cancelled appointment is emitted with STATUS:CANCELLED (not dropped) so a
subscribed calendar removes it on the next refresh.
"""

from datetime import UTC, datetime

from jbrain.appointments.service import AppointmentInfo

_PRODID = "-//JBrain//Appointments//EN"
# appointment.yaml Lifecycle → iCalendar STATUS (which has no "occurred").
_STATUS = {
    "confirmed": "CONFIRMED",
    "tentative": "TENTATIVE",
    "cancelled": "CANCELLED",
    "occurred": "CONFIRMED",
}


def _escape(text: str) -> str:
    """Escape a TEXT value (RFC 5545 §3.3.11): backslash, semicolon, comma,
    newline. A stray CR is dropped (it would corrupt the line structure)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 §3.1): a continuation begins
    with CRLF + a single space, so each wrapped physical line stays ≤75 octets.
    Folds on character boundaries (a multi-byte char is never split)."""
    segments: list[str] = []
    current = ""
    current_octets = 0
    for ch in line:
        size = len(ch.encode("utf-8"))
        # First physical line caps at 75; a continuation spends one octet on the
        # leading space, so its content caps at 74.
        cap = 75 if not segments else 74
        if current_octets + size > cap:
            segments.append(current)
            current, current_octets = ch, size
        else:
            current += ch
            current_octets += size
    segments.append(current)
    return "\r\n ".join(segments)


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _date(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%d")


def _vevent(appt: AppointmentInfo, *, stamp: str) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{appt.id}@jbrain",
        f"DTSTAMP:{stamp}",
    ]
    if appt.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{_date(appt.starts_at)}")
        if appt.ends_at:
            lines.append(f"DTEND;VALUE=DATE:{_date(appt.ends_at)}")
    else:
        lines.append(f"DTSTART:{_utc(appt.starts_at)}")
        if appt.ends_at:
            lines.append(f"DTEND:{_utc(appt.ends_at)}")
    lines.append(f"SUMMARY:{_escape(appt.title)}")
    if appt.location:
        lines.append(f"LOCATION:{_escape(appt.location)}")
    lines.append(f"STATUS:{_STATUS.get(appt.status, 'CONFIRMED')}")
    if appt.rrule:
        # RRULE is structured, not TEXT (no backslash-escaping), but a stray CR/LF
        # from a malformed token would break the line structure — strip them.
        rrule = appt.rrule.replace("\r", "").replace("\n", "")
        lines.append(f"RRULE:{rrule}")
    lines.append("END:VEVENT")
    return lines


def to_ics(appts: list[AppointmentInfo], *, now: datetime | None = None) -> str:
    """Serialize appointments to an iCalendar document (CRLF line endings, folded
    content lines). `now` stamps DTSTAMP; defaults to the current instant."""
    stamp = _utc(now or datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:JBrain",
    ]
    for appt in appts:
        lines.extend(_vevent(appt, stamp=stamp))
    lines.append("END:VCALENDAR")
    return "".join(f"{_fold(line)}\r\n" for line in lines)
