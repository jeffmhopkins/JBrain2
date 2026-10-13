import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PanelSettingValues } from "../api/client";
import { PanelSettings, ago, summary } from "./PanelSettings";

// The desk panel's settings on its card. The panel is the source of truth, so what has to be
// right: every value shown is what the panel reported, a change says "Waiting for the panel"
// until the panel reports it back, and the offline state says the change waits on the box.

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const URL = "/api/endpoint/panels/panel-desk/settings";
const REPORTED: PanelSettingValues = {
  night_on: 1,
  night_from: 1290,
  night_to: 390,
  night_color: 0,
  show_date: 1,
  tz: 5,
  notif_accept: 1,
  notif_quiet: 1,
  notif_chime: 1,
  brightness: 50,
  volume: 80,
  read_aloud: 1,
  auto_rotate: 1,
};
const ZONES = ["Honolulu", "Anchorage", "Los Angeles", "Denver", "Chicago", "New York"];

function fakeBox(opts: { online?: boolean; supported?: boolean } = {}) {
  const state = {
    reported: { ...REPORTED },
    pending: {} as PanelSettingValues,
    online: opts.online ?? true,
    supported: opts.supported ?? true,
    sent: [] as unknown[],
  };
  const handler = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (path !== URL) return json({ detail: "unexpected" }, 500);
    if (method === "PUT") {
      const { changes } = JSON.parse(String(init?.body)) as { changes: PanelSettingValues };
      state.sent.push(changes);
      Object.assign(state.pending, changes);
    }
    return json({
      supported: state.supported,
      reported: state.supported ? state.reported : null,
      reported_s: state.online ? 40 : 7200,
      online: state.online,
      pending: state.pending,
      version: state.sent.length,
      zones: ZONES,
      night_colors: ["Red", "Amber", "Grey"],
    });
  };
  /** The panel's next pickup: it took everything waiting and reports it back. */
  const pickup = () => {
    Object.assign(state.reported, state.pending);
    state.pending = {};
  };
  return { state, handler, pickup };
}

describe("the settings' words", () => {
  it("says how long ago in short units", () => {
    expect(ago(40)).toBe("40 s");
    expect(ago(180)).toBe("3 min");
    expect(ago(7200)).toBe("2 h");
    expect(ago(3 * 86400)).toBe("3 days");
  });

  it("summarises each group as the mock does", () => {
    const colours = ["Red", "Amber", "Grey"];
    expect(summary("clock", REPORTED, colours)).toBe("Night clock 21:30–06:30 · Red");
    expect(summary("clock", { ...REPORTED, night_on: 0 }, colours)).toBe("Night clock off");
    expect(summary("notices", REPORTED, colours)).toBe("Accepting · refused at night · chime");
    expect(summary("notices", { ...REPORTED, notif_accept: 0 }, colours)).toBe("Not accepting");
    expect(summary("screen", REPORTED, colours)).toBe("Brightness 50% · volume 80%");
  });
});

describe("the desk panel's settings", () => {
  let box: ReturnType<typeof fakeBox>;

  beforeEach(() => {
    box = fakeBox();
    vi.stubGlobal("fetch", vi.fn(box.handler));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("shows what the panel reported, folded into three groups", async () => {
    render(<PanelSettings deviceId="panel-desk" />);
    expect(await screen.findByText("On the panel")).toBeInTheDocument();
    expect(screen.getByText("As the panel reported them 40 s ago.")).toBeInTheDocument();
    expect(screen.getByText("Night clock 21:30–06:30 · Red")).toBeInTheDocument();
    expect(screen.getByText("Accepting · refused at night · chime")).toBeInTheDocument();
    expect(screen.getByText("Brightness 50% · volume 80%")).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Night clock" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Clock/ }));
    expect(screen.getByRole("switch", { name: "Night clock" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("combobox")).toHaveValue("5");
  });

  it("sends a change, says it waits for the panel, then that it is on the panel", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<PanelSettings deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("button", { name: /Clock/ }));
    fireEvent.click(screen.getByRole("button", { name: "Amber" }));
    expect(await screen.findByText("Waiting for the panel")).toBeInTheDocument();
    expect(box.state.sent).toEqual([{ night_color: 1 }]);
    expect(screen.getByText("Sending to panel…")).toBeInTheDocument();
    expect(screen.getByText("1 waiting")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Amber" })).toHaveAttribute("aria-pressed", "true");

    box.pickup();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    const header = screen.getAllByText("On the panel");
    expect(header.length).toBe(2); // the section's header and the colour row
    expect(screen.queryByText("Waiting for the panel")).toBeNull();
  });

  it("dims the night-hour switches when notices are not accepted", async () => {
    box.state.reported.notif_accept = 0;
    render(<PanelSettings deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("button", { name: /Notices/ }));
    expect(screen.getByRole("switch", { name: "Refuse during night hours" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Chime when one arrives" })).toBeDisabled();
    fireEvent.click(screen.getByRole("switch", { name: "Accept notices from the box" }));
    expect(await screen.findByText("Waiting for the panel")).toBeInTheDocument();
    expect(box.state.sent).toEqual([{ notif_accept: 1 }]);
  });

  it("sends a slider once it is still, not at every step", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<PanelSettings deviceId="panel-desk" />);
    fireEvent.click(await screen.findByRole("button", { name: /Screen and sound/ }));
    const slider = screen.getByRole("slider", { name: "Brightness" });
    fireEvent.change(slider, { target: { value: "40" } });
    fireEvent.change(slider, { target: { value: "30" } });
    expect(box.state.sent).toEqual([]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(box.state.sent).toEqual([{ brightness: 30 }]);
  });

  it("says the panel is offline and that changes wait on the box", async () => {
    box = fakeBox({ online: false });
    vi.stubGlobal("fetch", vi.fn(box.handler));
    render(<PanelSettings deviceId="panel-desk" />);
    expect(await screen.findByText("Panel offline")).toBeInTheDocument();
    expect(screen.getByText("Showing what the panel reported 2 h ago.")).toBeInTheDocument();
    expect(screen.getByText(/Changes wait on the box/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Screen and sound/ }));
    fireEvent.click(screen.getByRole("switch", { name: "Auto-rotate" }));
    expect(await screen.findByText("Waiting — the panel is offline")).toBeInTheDocument();
  });

  it("says when the panel's firmware does not report its settings", async () => {
    box = fakeBox({ supported: false });
    vi.stubGlobal("fetch", vi.fn(box.handler));
    const { container } = render(<PanelSettings deviceId="panel-desk" />);
    expect(await screen.findByText(/does not report its settings yet/)).toBeInTheDocument();
    expect(within(container).queryByRole("button", { name: /Clock/ })).toBeNull();
  });
});
