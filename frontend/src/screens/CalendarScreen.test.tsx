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
    render(<CalendarScreen onOpenNote={noop} />);
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
    render(<CalendarScreen onOpenNote={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => expect(screen.getByText("Dentist")).toBeInTheDocument());
    expect(screen.getByText("Pay rent")).toBeInTheDocument();
    // Relative day labels on the group headers (the nav "Today" button also exists).
    expect(screen.getByText("Today", { selector: ".cal-rel" })).toBeInTheDocument();
    expect(screen.getByText("Tomorrow", { selector: ".cal-rel" })).toBeInTheDocument();
  });

  it("opens an event sheet noting it's projected from notes", async () => {
    stubFetch([appt({ id: "A1", title: "Dentist", status: "tentative", start: today() })]);
    render(<CalendarScreen onOpenNote={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    expect(screen.getByText(/projected from your notes/i)).toBeInTheDocument();
    // Status renders as a flag, not a colour the model chose.
    expect(screen.getAllByText("tentative").length).toBeGreaterThan(0);
  });

  it("shows an empty state with no appointments", async () => {
    stubFetch([]);
    render(<CalendarScreen onOpenNote={noop} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => expect(screen.getByText("No upcoming appointments.")).toBeInTheDocument());
  });

  it("opens the source note from the event sheet when there is one", async () => {
    const onOpenNote = vi.fn();
    stubFetch([appt({ id: "A1", title: "Dentist", start: today(), source_note_id: "note-7" })]);
    render(<CalendarScreen onOpenNote={onOpenNote} />);
    fireEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    await waitFor(() => screen.getByText("Dentist"));
    fireEvent.click(screen.getByText("Dentist"));
    fireEvent.click(screen.getByText(/open the source note/i));
    expect(onOpenNote).toHaveBeenCalledWith("note-7");
  });
});
