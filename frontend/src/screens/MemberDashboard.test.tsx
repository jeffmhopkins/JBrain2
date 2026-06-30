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
let onPointSelect: ((fix: LocationFix | null) => void) | null = null;
const updateSpy = vi.fn((s: MapState) => {
  lastState = s;
});
const centerSpy = vi.fn();
const followSpy = vi.fn();
const schemeSpy = vi.fn();
const writeSchemeSpy = vi.fn();
vi.mock("./leafletMap", () => ({
  createLocationMap: (
    _el: HTMLElement,
    sel?: (id: string) => void,
    point?: (fix: LocationFix | null) => void,
  ) => {
    onSelect = sel ?? null;
    onPointSelect = point ?? null;
    return {
      update: updateSpy,
      centerOn: centerSpy,
      follow: followSpy,
      setScheme: schemeSpy,
      destroy: vi.fn(),
    };
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
  velocity_mps: null,
  course_deg: null,
  acceleration_mps2: null,
  altitude_m: null,
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
    velocity_mps: null,
    is_self: false,
    ...over,
  };
}

function deps(over: Partial<MemberDeps> = {}): MemberDeps {
  return {
    probe: vi.fn(
      async (): Promise<Principal> => ({ principal_id: "d1", kind: "device_key", label: "Me" }),
    ),
    listRoster: vi.fn(async () => [
      subject({ is_self: true }),
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
  followSpy.mockClear();
  schemeSpy.mockClear();
  writeSchemeSpy.mockClear();
  lastState = null;
  onSelect = null;
  onPointSelect = null;
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

  it("pans to follow the selected person as their live fix moves them", async () => {
    render(<MemberDashboard deps={deps()} />);
    // Focus self: its live fixes apply immediately (others are coalesced on a flush).
    fireEvent.click(await screen.findByRole("tab", { name: /Me/ }));
    await waitFor(() => expect(centerSpy).toHaveBeenCalledWith(40.0, -74.0));
    // The first focus centers; a subsequent fix for the same person pans (follows).
    act(() =>
      liveOnFix?.({
        subject_id: "s-me",
        lat: 40.5,
        lon: -74.5,
        accuracy_m: 10,
        battery_pct: 80,
        velocity_mps: 5,
        captured_at: new Date().toISOString(),
      }),
    );
    await waitFor(() => expect(followSpy).toHaveBeenCalledWith(40.5, -74.5));
    expect(centerSpy).toHaveBeenCalledTimes(1); // still just the one initial center
  });

  it("applies a native loopback fix for the viewer's own device", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Everyone/ });
    await waitFor(() => expect(lastState?.pins?.length).toBe(2));
    // The Android app pushes this phone's own fix straight into the page (no subject_id;
    // it's always self) — the self pin must move without waiting on the network round-trip.
    const w = window as Window & { __jbrainLocalFix?: (p: unknown) => void };
    expect(w.__jbrainLocalFix).toBeTypeOf("function");
    act(() =>
      w.__jbrainLocalFix?.({
        lat: 42.5,
        lon: -71.5,
        accuracy_m: 8,
        battery_pct: 77,
        velocity_mps: 9,
        captured_at: new Date().toISOString(),
      }),
    );
    await waitFor(() => {
      const me = lastState?.pins?.find((p) => p.subjectId === "s-me");
      expect(me?.lat).toBe(42.5);
      expect(me?.lon).toBe(-71.5);
    });
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

  it("refetches the trail when the window preset changes", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    await selectBob();
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    fireEvent.click(screen.getByRole("button", { name: "1h" }));
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(2));
    const sinceOf = (i: number) =>
      new Date(
        (d.listPositions as ReturnType<typeof vi.fn>).mock.calls[i]?.[1] as string,
      ).getTime();
    // 1h is narrower than the 1d default → a more-recent `since`.
    expect(sinceOf(1)).toBeGreaterThan(sinceOf(0));
  });

  it("sets the trail window to the picked preset (1h/8h/1d/7d)", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    await selectBob();
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(2));
    const sinceOf = (i: number) =>
      new Date(
        (d.listPositions as ReturnType<typeof vi.fn>).mock.calls[i]?.[1] as string,
      ).getTime();
    // 7d spans the full week: since ≈ now − 7 days, earlier than the 1d default.
    expect(sinceOf(1)).toBeLessThan(sinceOf(0));
    expect(Math.abs(sinceOf(1) - (Date.now() - 7 * 86_400_000))).toBeLessThan(120_000);
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

  it("shows battery and current speed in the dock for a moving person", async () => {
    const d = deps({
      listRoster: vi.fn(async () => [
        subject({ is_self: true }),
        subject({
          subject_id: "s-bob",
          label: "Bob",
          latitude: 41.0,
          longitude: -73.0,
          battery_pct: 64,
          velocity_mps: 13.4, // ≈ 30 mph
        }),
      ]),
    });
    render(<MemberDashboard deps={d} />);
    await selectBob();
    const dock = screen.getByRole("button", { name: /Bob/ });
    expect(dock.textContent).toMatch(/64%/);
    expect(dock.textContent).toMatch(/30 mph/);
  });

  it("opens the person's activity timeline from the Activity button", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    fireEvent.click(screen.getByRole("button", { name: /Activity/i }));
    expect(await screen.findByText(/recent activity/i)).toBeInTheDocument();
    expect(screen.getByText(/Arrived Home/)).toBeInTheDocument();
    expect(screen.getByText(/Left Work/)).toBeInTheDocument();
  });

  it("auto-shows current detail on select, clears it off-trail, re-shows on dock tap", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    // Selecting a person brings up their current detail card automatically.
    expect(await screen.findByText("Heading")).toBeInTheDocument();
    // A tap off the trail (map background) deselects → the card goes away.
    act(() => onPointSelect?.(null));
    await waitFor(() => expect(screen.queryByText("Heading")).toBeNull());
    // Tapping the focused person in the dock re-shows the current detail.
    fireEvent.click(screen.getByRole("button", { name: /Bob/ }));
    expect(await screen.findByText("Heading")).toBeInTheDocument();
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

  it("moves the viewer's own pin and extends its trail on every fix", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Bob/ }); // roster loaded + map wired
    act(() => onSelect?.("s-me")); // focus self
    await waitFor(() => expect(lastState?.fixes?.length).toBe(2));
    act(() =>
      liveOnFix?.({
        subject_id: "s-me",
        lat: 40.5,
        lon: -74.5,
        accuracy_m: 5,
        battery_pct: 70,
        velocity_mps: 13,
        captured_at: new Date().toISOString(),
      }),
    );
    // Self fixes apply immediately — no waiting on the coalescing flush.
    await waitFor(() => {
      expect(lastState?.fixes?.length).toBe(3);
      expect(lastState?.pins?.find((p) => p.subjectId === "s-me")?.lat).toBe(40.5);
    });
  });

  it("coalesces other people's live fixes to the periodic flush", async () => {
    vi.useFakeTimers();
    try {
      render(<MemberDashboard deps={deps()} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50); // resolve mount fetches + the rAF redraw
      });
      act(() => onSelect?.("s-bob"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50); // resolve the trail fetch
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50); // flush the post-fetch rAF redraw
      });
      expect(lastState?.fixes?.length).toBe(2);
      act(() =>
        liveOnFix?.({
          subject_id: "s-bob",
          lat: 41.5,
          lon: -73.5,
          accuracy_m: 5,
          battery_pct: 70,
          velocity_mps: null,
          captured_at: new Date().toISOString(),
        }),
      );
      // Not applied on the spot — an "other" is buffered until the 10 s flush.
      expect(lastState?.fixes?.length).toBe(2);
      expect(lastState?.pins?.find((p) => p.subjectId === "s-bob")?.lat).toBe(41.0);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000); // fire the coalescing flush
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(50); // flush the post-flush rAF redraw
      });
      expect(lastState?.fixes?.length).toBe(3);
      expect(lastState?.pins?.find((p) => p.subjectId === "s-bob")?.lat).toBe(41.5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("hides the trail legend on the heat map (it has no meaning there)", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    // The legend's color-metric trigger is present for the trail...
    expect(screen.getByRole("button", { name: "Trail color metric" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /History/ }));
    fireEvent.click(screen.getByRole("button", { name: "Heat" }));
    await waitFor(() => expect(lastState?.mode).toBe("heat"));
    // ...and gone once the view is the heat map.
    expect(screen.queryByRole("button", { name: "Trail color metric" })).not.toBeInTheDocument();
  });

  it("recolors the trail when a metric is picked from the legend dropdown", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    await waitFor(() => expect(lastState?.metric).toBe("speed"));
    fireEvent.click(screen.getByRole("button", { name: "Trail color metric" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Accel" }));
    await waitFor(() => expect(lastState?.metric).toBe("accel"));
  });

  it("inspects a tapped trail point in a card and on the map", async () => {
    render(<MemberDashboard deps={deps()} />);
    await selectBob();
    await waitFor(() => expect(onPointSelect).not.toBeNull());
    act(() =>
      onPointSelect?.(
        fix({ velocity_mps: 13.4, course_deg: 47, battery_pct: 64, acceleration_mps2: 1.8 }),
      ),
    );
    // The card fills in the readout...
    expect(await screen.findByText(/30 mph/)).toBeInTheDocument();
    expect(screen.getByText(/47°/)).toBeInTheDocument();
    // ...and the map gets a tinted callout for the same point.
    await waitFor(() => expect(lastState?.selected?.label).toMatch(/30 mph/));
  });

  it("trims the trail with the slider without refetching (snappy scrub)", async () => {
    const d = deps({
      listPositions: vi.fn(async () => [
        fix({ captured_at: new Date(Date.now() - 60 * 60_000).toISOString() }),
        fix({ captured_at: new Date(Date.now() - 30 * 60_000).toISOString() }),
        fix({ captured_at: new Date().toISOString() }),
      ]),
    });
    render(<MemberDashboard deps={d} />);
    await selectBob();
    await waitFor(() => expect(lastState?.fixes?.length).toBe(3));
    // Drag the start thumb forward → drops the oldest fix(es), in memory only.
    fireEvent.change(screen.getByLabelText("Window start"), { target: { value: "60" } });
    await waitFor(() => expect((lastState?.fixes?.length ?? 3) < 3).toBe(true));
    expect(d.listPositions).toHaveBeenCalledTimes(1); // never refetched on drag
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

  it("toggles the basemap between dark and light with one button, and persists it", async () => {
    render(<MemberDashboard deps={deps()} />);
    // Starts dark, so the single toggle offers "switch to light"; it drives the tile
    // scheme + remembers the choice, never the redraw path (the app chrome is unaffected).
    fireEvent.click(await screen.findByRole("button", { name: /Switch to light map/i }));
    await waitFor(() => expect(schemeSpy).toHaveBeenLastCalledWith("light"));
    expect(writeSchemeSpy).toHaveBeenLastCalledWith("light");
    // Now the same button offers "switch to dark".
    fireEvent.click(screen.getByRole("button", { name: /Switch to dark map/i }));
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

  it("shows the cached map (not the lock wall) when the probe fails offline", async () => {
    const onLine = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    const probe = vi.fn(async (): Promise<Principal> => {
      throw new Error("network");
    });
    render(<MemberDashboard deps={deps({ probe })} />);
    // The map mounts despite the failed probe; the "not signed in" wall never shows.
    expect(await screen.findByTestId("map-canvas")).toBeInTheDocument();
    expect(screen.queryByText(/not signed in/i)).toBeNull();
    // And the offline badge reassures that fixes are still being captured.
    expect(screen.getByText(/caching fixes/i)).toBeInTheDocument();
    onLine.mockRestore();
  });

  it("still locks a failed probe when actually online", async () => {
    const onLine = vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    const probe = vi.fn(async (): Promise<Principal> => {
      throw new Error("401");
    });
    render(<MemberDashboard deps={deps({ probe })} />);
    expect(await screen.findByText(/not signed in/i)).toBeInTheDocument();
    onLine.mockRestore();
  });

  it("drops the offline badge when connectivity returns", async () => {
    const onLine = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    render(<MemberDashboard deps={deps()} />);
    expect(await screen.findByText(/caching fixes/i)).toBeInTheDocument();
    onLine.mockReturnValue(true);
    act(() => window.dispatchEvent(new Event("online")));
    await waitFor(() => expect(screen.queryByText(/caching fixes/i)).toBeNull());
    onLine.mockRestore();
  });
});
