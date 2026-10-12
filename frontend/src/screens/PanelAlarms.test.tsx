import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PanelAlarm, PanelAlarmIn } from "../api/client";
import { PanelAlarms, dayBit, daysText, hm } from "./PanelAlarms";

// The editor for the desk panel's alarms. The owner sets them here and the panel only switches
// them, so what has to be right: every field reaches the box in the panel's own encoding, a
// delete is never one stray tap, and "On the panel" is said only when it is true.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const URL = "/api/endpoint/panels/panel-desk/alarms";

/** A box that keeps alarms, and a panel that has (or has not) fetched the latest version. */
function fakeBox(seed: PanelAlarm[] = []) {
  const state = { alarms: [...seed], version: 1, held: 1, next: 100, sent: [] as unknown[] };
  const handler = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? (JSON.parse(String(init.body)) as PanelAlarmIn) : null;
    if (method !== "GET") state.sent.push([method, path, body]);
    if (path === URL && method === "GET") {
      return json({
        etag: `v${state.version}`,
        alarms: state.alarms,
        max: 8,
        panel_etag: `v${state.held}`,
        panel_seen_s: 3,
      });
    }
    if (path === URL && method === "POST" && body) {
      const made = { id: state.next++, ...body };
      state.alarms.push(made);
      state.version++;
      return json(made, 201);
    }
    const one = new RegExp(`^${URL}/(\\d+)$`).exec(path);
    if (one && method === "PUT" && body) {
      const id = Number(one[1]);
      state.alarms = state.alarms.map((a) => (a.id === id ? { id, ...body } : a));
      state.version++;
      return json({ id, ...body });
    }
    if (one && method === "DELETE") {
      state.alarms = state.alarms.filter((a) => a.id !== Number(one[1]));
      state.version++;
      return new Response(null, { status: 204 });
    }
    return json({ detail: "unexpected" }, 500);
  };
  return { state, handler };
}

const WORK: PanelAlarm = { id: 1, at: 405, days: 0x1f, on: true, label: "Work", sound: "chime" };
const WEEKEND: PanelAlarm = { id: 2, at: 510, days: 0x60, on: false, label: "", sound: "rise" };

describe("the alarm words and numbers", () => {
  it("speaks the panel's mask in the mock's words", () => {
    expect(daysText(0)).toBe("Once");
    expect(daysText(0x7f)).toBe("Every day");
    expect(daysText(0x1f)).toBe("Weekdays");
    expect(daysText(0x60)).toBe("Weekends");
    expect(daysText(dayBit(0) | dayBit(3))).toBe("Sun, Wed");
  });

  it("maps the Sunday-first chips onto the panel's Monday-first bits", () => {
    expect(dayBit(0)).toBe(1 << 6); // Sunday
    expect(dayBit(1)).toBe(1 << 0); // Monday
    expect(dayBit(6)).toBe(1 << 5); // Saturday
  });

  it("is 24-hour", () => {
    expect(hm(405)).toBe("06:45");
    expect(hm(0)).toBe("00:00");
    expect(hm(1439)).toBe("23:59");
  });
});

describe("the desk panel's alarms", () => {
  let box: ReturnType<typeof fakeBox>;

  beforeEach(() => {
    box = fakeBox([WORK, WEEKEND]);
    vi.stubGlobal("fetch", vi.fn(box.handler));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("lists them in 24-hour time with their days, label and sound, and says they are on the panel", async () => {
    render(<PanelAlarms deviceId="panel-desk" />);
    expect(await screen.findByText("06:45")).toBeInTheDocument();
    expect(screen.getByText("Weekdays · Work · Chime")).toBeInTheDocument();
    expect(screen.getByText("Weekends · Rise")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Alarm 06:45, on" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("switch", { name: "Alarm 08:30, off" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(screen.getByText("On the panel")).toBeInTheDocument();
  });

  it("switches one off with the whole alarm, and waits for the panel to fetch it", async () => {
    render(<PanelAlarms deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("switch", { name: "Alarm 06:45, on" }));
    await screen.findByRole("switch", { name: "Alarm 06:45, off" });
    expect(box.state.sent).toEqual([
      ["PUT", `${URL}/1`, { at: 405, days: 0x1f, on: false, label: "Work", sound: "chime" }],
    ]);
    expect(screen.getByText("Sending to panel…")).toBeInTheDocument();
  });

  it("says 'On the panel' once the panel has fetched the new version", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<PanelAlarms deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("switch", { name: "Alarm 06:45, on" }));
    await screen.findByText("Sending to panel…");
    box.state.held = box.state.version; // the panel's minute poll
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(await screen.findByText("On the panel")).toBeInTheDocument();
  });

  it("adds an alarm with time, Sunday-first days, label and sound in the panel's encoding", async () => {
    box = fakeBox([]);
    vi.stubGlobal("fetch", vi.fn(box.handler));
    render(<PanelAlarms deviceId="panel-desk" />);
    expect(await screen.findByText(/No alarms yet/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add alarm" }));
    expect(screen.getByText("New alarm")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "21:00" } });
    // Weekdays are the default; clear them and pick Sunday and Saturday.
    for (const d of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
      fireEvent.click(screen.getByRole("button", { name: d }));
    }
    expect(screen.getByText(/rings once, then switches itself off/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sunday" }));
    fireEvent.click(screen.getByRole("button", { name: "Saturday" }));
    fireEvent.change(screen.getByLabelText("Label (optional)"), {
      target: { value: " Girls to bed " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Beeps" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("21:00")).toBeInTheDocument();
    expect(box.state.sent).toEqual([
      ["POST", URL, { at: 1260, days: 0x60, on: true, label: "Girls to bed", sound: "beeps" }],
    ]);
    expect(screen.queryByText("New alarm")).not.toBeInTheDocument();
  });

  it("edits an alarm's time and keeps the rest", async () => {
    render(<PanelAlarms deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit alarm 06:45" }));
    expect(screen.getByText("Edit alarm")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "06:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("06:30")).toBeInTheDocument();
    expect(box.state.sent).toEqual([
      ["PUT", `${URL}/1`, { at: 390, days: 0x1f, on: true, label: "Work", sound: "chime" }],
    ]);
  });

  it("deletes only on a second tap, and the arming lapses", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<PanelAlarms deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit alarm 06:45" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("button", { name: "Tap again to delete" })).toBeInTheDocument();
    expect(box.state.sent).toEqual([]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Tap again to delete" }));
    await waitFor(() => expect(screen.queryByText("06:45")).not.toBeInTheDocument());
    expect(box.state.sent).toEqual([["DELETE", `${URL}/1`, null]]);
  });

  it("refuses a label the panel cannot draw before it is sent", async () => {
    render(<PanelAlarms deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit alarm 06:45" }));
    fireEvent.change(screen.getByLabelText("Label (optional)"), { target: { value: "Café" } });
    expect(screen.getByText(/the panel cannot draw the rest/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("stops offering Add at eight", async () => {
    const eight = Array.from({ length: 8 }, (_, i) => ({ ...WORK, id: i + 1, at: i }));
    box = fakeBox(eight);
    vi.stubGlobal("fetch", vi.fn(box.handler));
    render(<PanelAlarms deviceId="panel-desk" />);
    await screen.findByText("00:07");
    expect(screen.getByRole("button", { name: "Add alarm" })).toBeDisabled();
    expect(screen.getByText("The panel holds 8 alarms.")).toBeInTheDocument();
  });

  it("says what the box said when a save is refused, and keeps the form", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if ((init?.method ?? "GET") === "GET") return box.handler(input, init);
        return json({ detail: "the panel holds 8 alarms" }, 409);
      }),
    );
    render(<PanelAlarms deviceId="panel-desk" />);
    await screen.findByText("06:45");
    fireEvent.click(screen.getByRole("button", { name: "Add alarm" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("the panel holds 8 alarms")).toBeInTheDocument();
    expect(screen.getByText("New alarm")).toBeInTheDocument();
  });

  it("says so when the alarms cannot be read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ detail: "box down" }, 500)),
    );
    render(<PanelAlarms deviceId="panel-desk" />);
    expect(await screen.findByText("box down")).toBeInTheDocument();
    const add = screen.getByRole("button", { name: "Add alarm" });
    expect(add).toBeDisabled();
    expect(within(add.parentElement as HTMLElement).queryByText("On the panel")).toBeNull();
  });
});
