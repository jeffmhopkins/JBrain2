"""The agent's appointment read tools: list and read the owner's appointments.

Appointments are a projection of the appointment entities the extractor writes
(notes are the sole sources of truth, #7) — so these are READ-only: the agent
cannot write here directly (creating/moving an appointment stages a Proposal in
PR4). Every handler runs on `ToolContext.session`, so a narrowed session only
ever sees in-scope appointments. Ids ride in the model-facing text so the model
can chain (read_appointments → read_appointment); the prose it shows the owner
shouldn't paste them — the app renders the card.
"""

import uuid
from datetime import UTC, datetime

from jbrain.agent.contracts import ProposalRef, ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.appointments.service import AppointmentInfo, AppointmentsRepo

_WHEN_FMT = "%Y-%m-%d %H:%M"
_ACTIONS = ("create", "reschedule", "cancel")
_TITLE_LEN = 80


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


def _compose_body(action: str, title: str, when: str, location: str) -> str:
    """The note an approved appointment change re-enters as — a plain statement the
    extractor turns into a scheduledTime (or cancellation) on the appointment
    entity. The wording names the appointment so re-extraction resolves to the
    SAME entity (a reschedule supersedes its time; it never forks a new one)."""
    at = f" at {location}" if location else ""
    if action == "cancel":
        return f"The {title} has been cancelled."
    if action == "reschedule":
        return f"The {title} has been rescheduled to {when}{at}."
    return f"{title} is scheduled for {when}{at}."


def build_appointment_write_handlers(
    proposals: ProposalRepo, appointments: AppointmentsRepo
) -> dict[str, ToolHandler]:
    """The write path: manage_appointment STAGES a Proposal, it never writes. On
    approval the leaf re-enters as an agent note (the existing note executor), so
    the change flows through extraction → the appointment entity → the projection
    — appointments stay derived from notes (#7)."""

    async def manage_appointment_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        action = str(arguments.get("action", "")).strip().lower()
        if action not in _ACTIONS:
            return ToolOutput("manage_appointment action must be create, reschedule, or cancel.")
        if not ctx.session.principal_id:
            return ToolOutput("can't stage an appointment change without an owner principal.")

        # Anchor on an existing appointment when given: use its real title and
        # domain so the re-entered note resolves to that same entity.
        title = str(arguments.get("title", "")).strip()
        domain = str(arguments.get("domain", "")).strip()
        appt_id = str(arguments.get("appointment_id", "")).strip()
        if appt_id:
            existing = await appointments.get_appointment(ctx.session, appt_id)
            if existing is None:
                return ToolOutput("No appointment with that id is in scope.")
            title = title or existing.title
            domain = domain or existing.domain
        domain = domain or (ctx.scopes[0] if ctx.scopes else "general")

        when = str(arguments.get("when", "")).strip()
        location = str(arguments.get("location", "")).strip()
        if not title:
            return ToolOutput("manage_appointment needs a title (or an appointment_id).")
        if action in ("create", "reschedule") and not when:
            return ToolOutput(f"manage_appointment {action} needs a 'when' date/time.")
        if ctx.scopes and domain not in ctx.scopes:
            return ToolOutput(
                f"can't change an appointment in '{domain}' — this session isn't scoped to it."
            )

        body = _compose_body(action, title, when, location)
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="manage_appointment",
            label=body[:_TITLE_LEN],
            # `body`/`domain` are what the note executor enacts; the rest lets the
            # review surface render the appointment change without re-parsing it.
            preview={
                "body": body,
                "domain": domain,
                "action": action,
                "title": title,
                "when": when,
                "location": location or None,
                "appointment_id": appt_id or None,
            },
        )
        spec = ProposalSpec(
            kind="appointment",
            domain=domain,
            title=body[:_TITLE_LEN],
            nodes=[node],
            provenance={"source": "chat", "action": action},
        )
        prop_id = await proposals.stage(
            ctx.session, principal_id=ctx.session.principal_id, spec=spec
        )
        verb = {"create": "add", "reschedule": "move", "cancel": "cancel"}[action]
        return ToolOutput(
            f"Staged a request to {verb} this appointment for your approval. I won't change"
            " your calendar until you approve it — it then re-enters as a normal, dated note.",
            proposal=ProposalRef(proposal_id=prop_id, kind="appointment"),
        )

    return {"manage_appointment": manage_appointment_tool}
