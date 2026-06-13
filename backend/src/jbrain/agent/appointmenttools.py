"""The agent's appointment read tools: list and read the owner's appointments.

Appointments are a projection of the appointment entities the extractor writes
(notes are the sole sources of truth, #7) — so these are READ-only: the agent
cannot write here directly (creating/moving an appointment stages a Proposal in
PR4). Every handler runs on `ToolContext.session`, so a narrowed session only
ever sees in-scope appointments. Ids ride in the model-facing text so the model
can chain (read_appointments → read_appointment); the prose it shows the owner
shouldn't paste them — the app renders the card.
"""

from datetime import UTC, datetime

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.appointments.service import AppointmentInfo, AppointmentsRepo

_WHEN_FMT = "%Y-%m-%d %H:%M"


def _when(appt: AppointmentInfo) -> str:
    """The appointment's time as stored (UTC); the card localizes for the owner."""
    if appt.all_day:
        return appt.starts_at.strftime("%Y-%m-%d") + " (all day)"
    start = appt.starts_at.strftime(_WHEN_FMT)
    return start + (f"–{appt.ends_at.strftime('%H:%M')}" if appt.ends_at else "")


def _tags(appt: AppointmentInfo) -> str:
    tags = []
    if appt.recurring:
        tags.append("recurring")
    if appt.status != "confirmed":
        tags.append(appt.status)
    return f" ({', '.join(tags)})" if tags else ""


def format_appointments(appts: list[AppointmentInfo]) -> str:
    """The model-facing index — title, when, domain, status/recurrence, and id."""
    if not appts:
        return "No appointments in scope."
    return "\n".join(f"- {a.title} — {_when(a)} [{a.domain}]{_tags(a)} id={a.id}" for a in appts)


def format_appointment(appt: AppointmentInfo) -> str:
    """One appointment in full — for the model to read before answering or acting."""
    lines = [f"{appt.title} [{appt.domain}]", f"when: {_when(appt)}", f"status: {appt.status}"]
    if appt.location:
        lines.append(f"location: {appt.location}")
    if appt.rrule:
        lines.append(f"repeats: {appt.rrule}")
    if appt.attendees:
        names = ", ".join(str(p.get("name", "")) for p in appt.attendees if p.get("name"))
        if names:
            lines.append(f"with: {names}")
    return "\n".join(lines)


def appointment_card(appt: AppointmentInfo) -> ViewPayload:
    """The structured twin of format_appointment: an `appointment_card` the PWA
    renders (data-only slots, never model-authored markup). Times are ISO strings
    so the frontend localizes them to the owner's zone."""
    return ViewPayload(
        view="appointment_card",
        surface="inline",
        data={
            "id": appt.id,
            "title": appt.title,
            "domain": appt.domain,
            "start": appt.starts_at.isoformat(),
            "end": appt.ends_at.isoformat() if appt.ends_at else None,
            "all_day": appt.all_day,
            "location": appt.location,
            "status": appt.status,
            "rrule": appt.rrule,
            "recurring": appt.recurring,
            "attendees": [str(p.get("name", "")) for p in appt.attendees if p.get("name")],
        },
    )


def build_appointment_handlers(appointments: AppointmentsRepo) -> dict[str, ToolHandler]:
    async def read_appointments_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        include_past = bool(arguments.get("include_past", False))
        include_cancelled = bool(arguments.get("include_cancelled", False))
        # Default to what's ahead: an agent answering "what's on my calendar"
        # wants upcoming items, not a lifetime of history.
        since = None if include_past else datetime.now(UTC)
        rows = await appointments.list_appointments(
            ctx.session, since=since, include_cancelled=include_cancelled
        )
        return ToolOutput(format_appointments(rows))

    async def read_appointment_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        appt_id = str(arguments.get("appointment_id", "")).strip()
        if not appt_id:
            return ToolOutput("read_appointment needs an appointment_id.")
        appt = await appointments.get_appointment(ctx.session, appt_id)
        if appt is None:
            return ToolOutput("No appointment with that id is in scope.")
        # The text is the model's; the card is the owner's tappable appointment.
        return ToolOutput(format_appointment(appt), view=appointment_card(appt))

    return {
        "read_appointments": read_appointments_tool,
        "read_appointment": read_appointment_tool,
    }
