import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppointmentOut } from "../api/client";
import { CalendarScreen } from "./CalendarScreen";

function appt(over: Partial<AppointmentOut>): AppointmentOut {
  return {
    id: "A1",
    title: "Dentist",
    domain: "health",
    start: new Date().toISOString(),
    end: null,
    all_day: false,
    status: "confirmed",
    location: null,
    organizer: null,
    attendance_mode: null,
    online_url: null,
    description: null,
    appointment_type: null,
    rrule: null,
    recurring: false,
    attendees: [],
    source_note_id: null,
    ...over,
  };
}

const noop = () => {};

function stubFetch(events: AppointmentOut[]) {
  const m = vi.fn<typeof fetch>(async (input) => {
    const path = String(input);
    if (path.startsWith("/api/appointments")) {
      return new Response(JSON.stringify(events), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // The event sheet pulls the source note for an inline snippet.
    if (path.startsWith("/api/notes/")) {
      return new Response(JSON.stringify({ id: "note-7", body: "the source note body" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    throw new Error(`Unexpected fetch: ${path}`);
  });
  vi.stubGlobal("fetch", m);
}

afterEach(() => vi.unstubAllGlobals());

// A timed event today and an all-day one tomorrow, so today's agenda and the
// chronological Tasks list both have content.
function today(h = 10): string {
  const d = new Date();
  d.setHours(h, 0, 0, 0);
  return d.toISOString();
}
function tomorrow(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
}

describe("CalendarScreen", () => {
  it("defaults to month and shows today's agenda", async () => {
    stubFetch([appt({ id: "A1", title: "Dentist", start: today() })]);
    render(<CalendarScreen onOpenNote={noop} onCompose={noop} />);
    // Month is the default tab.
    expect(screen.getByRole("tab", { name: "Month" })).toHaveAttribute("aria-selected", "true");
    // Today is selected, so its agenda lists the appointment.
    await waitFor(() => expect(screen.getByText("Dentist")).toBeInTheDocument());
  });

  it("lists upcoming items chronologically in Tasks", async () => {
    stubFetch([
      appt({ id: "A1", title: "Dentist", start: today() }),
      appt({ id: "A2", title: "Pay rent", domain: "finance", all_day: true, start: tomorrow() }),
    ]);
    render(<CalendarScreen onOpenNote={noop} onCompose={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => expect(screen.getByText("Dentist")).toBeInTheDocument());
    expect(screen.getByText("Pay rent")).toBeInTheDocument();
    // Relative day labels on the group headers (the nav "Today" button also exists).
    expect(screen.getByText("Today", { selector: ".cal-rel" })).toBeInTheDocument();
    expect(screen.getByText("Tomorrow", { selector: ".cal-rel" })).toBeInTheDocument();
  });

  it("opens an event sheet noting it's projected from notes", async () => {
    stubFetch([appt({ id: "A1", title: "Dentist", status: "tentative", start: today() })]);
    render(<CalendarScreen onOpenNote={noop} onCompose={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    expect(screen.getByText(/projected from your notes/i)).toBeInTheDocument();
    // Status renders as a flag, not a colour the model chose.
    expect(screen.getAllByText("tentative").length).toBeGreaterThan(0);
  });

  it("shows an empty state with no appointments", async () => {
    stubFetch([]);
    render(<CalendarScreen onOpenNote={noop} onCompose={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => expect(screen.getByText("No upcoming appointments.")).toBeInTheDocument());
  });

  it("renders the where/who facets in the event sheet", async () => {
    stubFetch([
      appt({
        id: "A1",
        title: "Dentist",
        start: today(),
        location: "Maple Dental",
        organizer: "Maple Dental Group",
        attendance_mode: "online",
        online_url: "https://meet.example/abc",
        description: "Bring x-rays",
        appointment_type: "checkup",
        attendees: [
          {
            name: "Dr. Nguyen",
            entity_id: "p1",
            role: "chair",
            status: "accepted",
            required: true,
          },
          { name: "Pat", entity_id: null, role: null, status: "declined", required: false },
        ],
      }),
    ]);
    render(<CalendarScreen onOpenNote={noop} onCompose={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    expect(screen.getByText("Maple Dental")).toBeInTheDocument();
    expect(screen.getByText("hosted by Maple Dental Group")).toBeInTheDocument();
    expect(screen.getByText("checkup")).toBeInTheDocument();
    expect(screen.getByText("Bring x-rays")).toBeInTheDocument();
    // The join link, and attendees with a surfaced RSVP decline.
    const join = screen.getByRole("link", { name: "Join the meeting" });
    expect(join).toHaveAttribute("href", "https://meet.example/abc");
    expect(screen.getByText(/with Dr\. Nguyen, Pat \(declined\)/)).toBeInTheDocument();
  });

  it("opens the source note from the event sheet when there is one", async () => {
    const onOpenNote = vi.fn();
    stubFetch([appt({ id: "A1", title: "Dentist", start: today(), source_note_id: "note-7" })]);
    render(<CalendarScreen onOpenNote={onOpenNote} onCompose={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("open note"));
    expect(onOpenNote).toHaveBeenCalledWith("note-7");
  });

  it("reschedule opens a date/time modal that hands off with the appointment id", async () => {
    const onCompose = vi.fn();
    stubFetch([appt({ id: "A1", title: "Dentist", start: today() })]);
    render(<CalendarScreen onOpenNote={noop} onCompose={onCompose} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("reschedule"));
    // The modal — not the omnibox — collects the new time before handing off.
    const submit = screen.getByRole("button", { name: "Reschedule" });
    fireEvent.click(submit);
    expect(onCompose).toHaveBeenCalledWith(expect.stringContaining('Reschedule my "Dentist"'), {
      id: "A1",
      title: "Dentist",
    });
  });

  it("cancel is an armed two-tap before it hands off with the appointment id", async () => {
    const onCompose = vi.fn();
    stubFetch([appt({ id: "A1", title: "Dentist", start: today() })]);
    render(<CalendarScreen onOpenNote={noop} onCompose={onCompose} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("cancel"));
    expect(onCompose).not.toHaveBeenCalled(); // first tap only arms
    fireEvent.click(screen.getByText("tap again to cancel"));
    expect(onCompose).toHaveBeenCalledWith(expect.stringContaining('Cancel my "Dentist"'), {
      id: "A1",
      title: "Dentist",
    });
  });

  it("add to calendar downloads the .ics instead of navigating", async () => {
    stubFetch([appt({ id: "A1", title: "Dentist", start: today() })]);
    // jsdom has no object-URL plumbing — stand in the two statics the download uses.
    const createObjectURL = vi.fn(() => "blob:x");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<CalendarScreen onOpenNote={noop} onCompose={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("add to calendar"));
    // A blob is fetched and clicked through — never an in-place navigation.
    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });
});
