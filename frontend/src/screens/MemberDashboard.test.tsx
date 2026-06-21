import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  LocationFix,
  MemberSubject,
  PlaceGeofence,
  Principal,
  TimelineEntry,
} from "../api/client";
import { MemberDashboard, type MemberDeps } from "./MemberDashboard";
import type { MapState } from "./leafletMap";
import type { LiveFix } from "./liveSocket";

// Stand in for the Leaflet wrapper: capture each update() state, the centerOn calls,
// and the pin-tap callback — so the tests assert what the screen drives, no real map.
let lastState: MapState | null = null;
let onSelect: ((id: string) => void) | null = null;
const updateSpy = vi.fn((s: MapState) => {
  lastState = s;
});
const centerSpy = vi.fn();
const schemeSpy = vi.fn();
const writeSchemeSpy = vi.fn();
vi.mock("./leafletMap", () => ({
  createLocationMap: (_el: HTMLElement, sel?: (id: string) => void) => {
    onSelect = sel ?? null;
    return { update: updateSpy, centerOn: centerSpy, setScheme: schemeSpy, destroy: vi.fn() };
  },
  readTileScheme: () => "dark",
  writeTileScheme: (s: string) => writeSchemeSpy(s),
}));

// Capture the live-socket callback so a test can push a fix.
let liveOnFix: ((fix: LiveFix) => void) | null = null;
vi.mock("./liveSocket", () => ({
  connectLive: (cb: (fix: LiveFix) => void) => {
    liveOnFix = cb;
    return { close: vi.fn() };
  },
}));

const fix = (over: Partial<LocationFix> = {}): LocationFix => ({
  captured_at: new Date().toISOString(),
  latitude: 41.0,
  longitude: -73.0,
  accuracy_m: 10,
  battery_pct: 80,
  ...over,
});

const crossing = (over: Partial<TimelineEntry> = {}): TimelineEntry => ({
  occurred_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  subject_id: "s-bob",
  transition: "enter",
  place_entity_id: "p-home",
  place_name: "Home",
  ...over,
});

function subject(over: Partial<MemberSubject> = {}): MemberSubject {
  return {
    subject_id: "s-me",
    label: "Me",
    last_seen: new Date(Date.now() - 60_000).toISOString(),
    battery_pct: 80,
    connection: "wifi",
    latitude: 40.0,
    longitude: -74.0,
    ...over,
  };
}

function deps(over: Partial<MemberDeps> = {}): MemberDeps {
  return {
    probe: vi.fn(
      async (): Promise<Principal> => ({ principal_id: "d1", kind: "device_key", label: "Me" }),
    ),
    listRoster: vi.fn(async () => [
      subject(),
      subject({ subject_id: "s-bob", label: "Bob", latitude: 41.0, longitude: -73.0 }),
    ]),
    listPlaces: vi.fn(async (): Promise<PlaceGeofence[]> => []),
    listPositions: vi.fn(async () => [fix(), fix({ latitude: 41.01, longitude: -73.01 })]),
    listTimeline: vi.fn(async () => [
      crossing(),
      crossing({
        occurred_at: new Date(Date.now() - 60 * 60_000).toISOString(),
        transition: "exit",
        place_name: "Work",
      }),
    ]),
    ...over,
  };
}

beforeEach(() => {
  updateSpy.mockClear();
  centerSpy.mockClear();
  schemeSpy.mockClear();
  writeSchemeSpy.mockClear();
  lastState = null;
  onSelect = null;
  liveOnFix = null;
});

const selectBob = async () => fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));

describe("MemberDashboard", () => {
  it("locks out a non-member session", async () => {
    const probe = vi.fn(
      async (): Promise<Principal> => ({ principal_id: "o", kind: "owner", label: "O" }),
    );
    render(<MemberDashboard deps={deps({ probe })} />);
    expect(await screen.findByText(/not signed in/i)).toBeInTheDocument();
  });

  it("shows the switcher and pins everyone (auto-fit) with the dock collapsed", async () => {
    render(<MemberDashboard deps={deps()} />);
    expect(await screen.findByRole("tab", { name: /Everyone/ })).toBeInTheDocument();
    await waitFor(() => expect(lastState?.pins?.length).toBe(2));
    expect(lastState?.autoFit).toBe(true);
    // Collapsed by default: the dock's person area (the Details trigger) is closed,
    // and History is disabled in Everyone.
    expect(screen.getByRole("button", { name: /Everyone/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("button", { name: /History/ })).toBeDisabled();
  });

  it("centers the map on a person and shows just their pin (no auto-fit)", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    await waitFor(() => expect(centerSpy).toHaveBeenCalledWith(41.0, -73.0));
    await waitFor(() => {
      expect(lastState?.pins?.length).toBe(1);
      expect(lastState?.pins?.[0]?.subjectId).toBe("s-bob");
      expect(lastState?.autoFit).toBe(false);
    });
  });

  it("follows a pin tap on the map back to the switcher", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Everyone/ });
    expect(onSelect).toBeTruthy();
    act(() => onSelect?.("s-bob"));
    await waitFor(() => expect(centerSpy).toHaveBeenCalledWith(41.0, -73.0));
  });

  it("pulls up History and toggles Heat (focused person)", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    const history = screen.getByRole("button", { name: /History/ });
    fireEvent.click(history);
    expect(history).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "Heat" }));
    await waitFor(() => expect(lastState?.mode).toBe("heat"));
  });

  it("refetches the trail when the time window changes", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    await selectBob();
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    fireEvent.change(screen.getByLabelText("Window start"), { target: { value: "0" } });
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(2));
    // The second call asks for a wider (earlier) since.
    const sinceOf = (i: number) =>
      new Date(
        (d.listPositions as ReturnType<typeof vi.fn>).mock.calls[i]?.[1] as string,
      ).getTime();
    expect(sinceOf(1)).toBeLessThan(sinceOf(0));
  });

  it("resizes the window to the picked total range (1/3/7 days)", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    await selectBob();
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    fireEvent.click(screen.getByRole("button", { name: "1d" }));
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(2));
    const sinceOf = (i: number) =>
      new Date(
        (d.listPositions as ReturnType<typeof vi.fn>).mock.calls[i]?.[1] as string,
      ).getTime();
    // Picking 1d spans the full one-day range: since ≈ now − 1 day, more recent than
    // the 3-day default, and the dual slider now covers the whole chosen range.
    expect(sinceOf(1)).toBeGreaterThan(sinceOf(0));
    expect(Math.abs(sinceOf(1) - (Date.now() - 86_400_000))).toBeLessThan(120_000);
  });

  it("tunes the heat radius and weight from the expanded pane", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    fireEvent.click(screen.getByRole("button", { name: "Heat" }));
    await waitFor(() => expect(lastState?.mode).toBe("heat"));
    fireEvent.change(screen.getByLabelText("Heat spot radius"), { target: { value: "40" } });
    await waitFor(() => expect(lastState?.heatRadius).toBe(40));
    fireEvent.change(screen.getByLabelText("Heat fix weight"), { target: { value: "0.8" } });
    await waitFor(() => expect(lastState?.heatWeight).toBe(0.8));
  });

  it("pulls up Details by tapping the person area of the dock bar", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    // Tapping the dock's person area (not a separate Details button) opens Details.
    fireEvent.click(screen.getByRole("button", { name: /Bob/ }));
    expect(await screen.findByText(/recent activity/i)).toBeInTheDocument();
    expect(screen.getByText(/Arrived Home/)).toBeInTheDocument();
    expect(screen.getByText(/Left Work/)).toBeInTheDocument();
  });

  it("loads the focused person's trail onto the map", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    await selectBob();
    await waitFor(() =>
      expect(d.listPositions).toHaveBeenCalledWith("s-bob", expect.any(String), expect.any(String)),
    );
    await waitFor(() => {
      expect(lastState?.mode).toBe("trail");
      expect(lastState?.fixes?.length).toBe(2);
    });
  });

  it("moves a pin live and extends the focused trail", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    await waitFor(() => expect(lastState?.fixes?.length).toBe(2));
    act(() =>
      liveOnFix?.({
        subject_id: "s-bob",
        lat: 41.5,
        lon: -73.5,
        accuracy_m: 5,
        battery_pct: 70,
        captured_at: new Date().toISOString(),
      }),
    );
    await waitFor(() => {
      expect(lastState?.fixes?.length).toBe(3);
      expect(lastState?.pins?.find((p) => p.subjectId === "s-bob")?.lat).toBe(41.5);
    });
  });

  it("clears the trail when its fetch fails", async () => {
    const listPositions = vi.fn(async (): Promise<LocationFix[]> => {
      throw new Error("offline");
    });
    render(<MemberDashboard deps={deps({ listPositions })} />);
    await selectBob();
    await waitFor(() => expect(lastState?.fixes?.length).toBe(0));
  });

  it("summarises the selected person in the dock bar", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    // The persistent bar shows the focused person's live/last-seen line.
    expect(await screen.findByText(/Live ·/)).toBeInTheDocument();
  });

  it("collapses a sheet on a second tap and on returning to Everyone", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    const history = screen.getByRole("button", { name: /History/ });
    fireEvent.click(history);
    expect(history).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(history); // second tap collapses
    expect(history).toHaveAttribute("aria-expanded", "false");
    // Re-open, then return to Everyone → History collapses and is disabled.
    fireEvent.click(history);
    fireEvent.click(screen.getByRole("tab", { name: /Everyone/ }));
    const historyAll = screen.getByRole("button", { name: /History/ });
    expect(historyAll).toBeDisabled();
    expect(historyAll).toHaveAttribute("aria-expanded", "false");
  });

  it("surfaces a load failure without crashing the map", async () => {
    const listRoster = vi.fn(async (): Promise<MemberSubject[]> => {
      throw new Error("offline");
    });
    render(<MemberDashboard deps={deps({ listRoster })} />);
    expect(await screen.findByText(/couldn't load the family/i)).toBeInTheDocument();
  });

  it("handles an empty roster", async () => {
    render(<MemberDashboard deps={deps({ listRoster: vi.fn(async () => []) })} />);
    expect(await screen.findByText(/no one to show yet/i)).toBeInTheDocument();
    await waitFor(() => expect(lastState?.pins?.length).toBe(0));
  });

  it("toggles only the basemap tiles between dark and light, and persists it", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Everyone/ });
    // Switching to Light drives the map's tile scheme and remembers the choice;
    // it never touches the redraw path (update), so the app chrome is unaffected.
    fireEvent.click(screen.getByRole("tab", { name: /Light map/i }));
    await waitFor(() => expect(schemeSpy).toHaveBeenLastCalledWith("light"));
    expect(writeSchemeSpy).toHaveBeenLastCalledWith("light");
    fireEvent.click(screen.getByRole("tab", { name: /Dark map/i }));
    await waitFor(() => expect(schemeSpy).toHaveBeenLastCalledWith("dark"));
    expect(writeSchemeSpy).toHaveBeenLastCalledWith("dark");
  });

  it("lists a fix-less member but doesn't pin them", async () => {
    const listRoster = vi.fn(async () => [
      subject(),
      subject({
        subject_id: "s-nofix",
        label: "Pat",
        last_seen: null,
        latitude: null,
        longitude: null,
      }),
    ]);
    render(<MemberDashboard deps={deps({ listRoster })} />);
    expect(await screen.findByRole("tab", { name: /Pat/ })).toBeInTheDocument();
    await waitFor(() => expect(lastState?.pins?.map((p) => p.subjectId)).toEqual(["s-me"]));
  });
});
