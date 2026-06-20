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

// Stand in for the Leaflet wrapper: capture each update() state + the pin-tap
// callback, so the tests assert the pins the screen drives without a real map.
let lastState: MapState | null = null;
let onSelect: ((id: string) => void) | null = null;
const updateSpy = vi.fn((s: MapState) => {
  lastState = s;
});
vi.mock("./leafletMap", () => ({
  createLocationMap: (_el: HTMLElement, sel?: (id: string) => void) => {
    onSelect = sel ?? null;
    return { update: updateSpy, destroy: vi.fn() };
  },
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
  lastState = null;
  onSelect = null;
  liveOnFix = null;
});

describe("MemberDashboard", () => {
  it("locks out a non-member session", async () => {
    const probe = vi.fn(
      async (): Promise<Principal> => ({ principal_id: "o", kind: "owner", label: "O" }),
    );
    render(<MemberDashboard deps={deps({ probe })} />);
    expect(await screen.findByText(/not signed in/i)).toBeInTheDocument();
  });

  it("shows the family switcher and pins everyone on the map", async () => {
    render(<MemberDashboard deps={deps()} />);
    // Switcher: Everyone + each member.
    expect(await screen.findByRole("tab", { name: /Everyone/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Me/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Bob/ })).toBeInTheDocument();
    // The map is driven with both located people's pins.
    await waitFor(() => expect(lastState?.pins?.length).toBe(2));
    expect(lastState?.pins?.map((p) => p.subjectId).sort()).toEqual(["s-bob", "s-me"]);
  });

  it("focuses one person on selection — card + a single pin", async () => {
    render(<MemberDashboard deps={deps()} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    await waitFor(() => {
      expect(lastState?.pins?.length).toBe(1);
      expect(lastState?.pins?.[0]?.subjectId).toBe("s-bob");
      expect(lastState?.pins?.[0]?.selected).toBe(true);
    });
    // The card switched from the Everyone roster to the focused person.
    expect(screen.queryByText(/Everyone ·/)).toBeNull();
  });

  it("follows a pin tap on the map back to the switcher", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Everyone/ });
    expect(onSelect).toBeTruthy();
    act(() => onSelect?.("s-bob"));
    await waitFor(() => expect(lastState?.pins?.[0]?.subjectId).toBe("s-bob"));
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
    // Only the Everyone chip; nothing to pin.
    expect(screen.getByRole("tab", { name: /Everyone/ })).toBeInTheDocument();
    await waitFor(() => expect(lastState?.pins?.length).toBe(0));
  });

  it("lists a fix-less member in the switcher but doesn't pin them", async () => {
    const listRoster = vi.fn(async () => [
      subject(),
      subject({
        subject_id: "s-nofix",
        label: "Pat",
        last_seen: null,
        battery_pct: null,
        latitude: null,
        longitude: null,
      }),
    ]);
    render(<MemberDashboard deps={deps({ listRoster })} />);
    // Pat appears in the switcher…
    expect(await screen.findByRole("tab", { name: /Pat/ })).toBeInTheDocument();
    // …but with no coordinate, only the located member is pinned.
    await waitFor(() => expect(lastState?.pins?.map((p) => p.subjectId)).toEqual(["s-me"]));
  });

  it("loads the focused person's trail and shows the Trail/Heat controls", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    // Controls appear only on a focus.
    expect(await screen.findByRole("button", { name: "Trail" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Heat" })).toBeInTheDocument();
    // The trail is fetched for that subject and driven to the map.
    await waitFor(() =>
      expect(d.listPositions).toHaveBeenCalledWith("s-bob", expect.any(String), expect.any(String)),
    );
    await waitFor(() => {
      expect(lastState?.mode).toBe("trail");
      expect(lastState?.fixes?.length).toBe(2);
    });
  });

  it("toggles Heat and changes the day range (refetching)", async () => {
    const d = deps();
    render(<MemberDashboard deps={d} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Heat" }));
    await waitFor(() => expect(lastState?.mode).toBe("heat"));

    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Days of history"), { target: { value: "7" } });
    await waitFor(() => expect(d.listPositions).toHaveBeenCalledTimes(2));
    expect(screen.getByText(/Last/)).toHaveTextContent("7 days");
  });

  it("expands the last-actions timeline for the focused person", async () => {
    render(<MemberDashboard deps={deps()} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    // The card head (a button) expands the timeline.
    fireEvent.click(await screen.findByRole("button", { name: /Bob/ }));
    // "Recent activity" is unique to the expanded view; the actions appear in both
    // the quick chips and the timeline.
    expect(await screen.findByText(/Recent activity/)).toBeInTheDocument();
    expect(screen.getAllByText(/Arrived Home/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Left Work/).length).toBeGreaterThanOrEqual(1);
  });

  it("hides the controls in Everyone mode", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Everyone/ });
    expect(screen.queryByRole("button", { name: "Trail" })).toBeNull();
  });

  it("moves a pin live when a fix arrives", async () => {
    render(<MemberDashboard deps={deps()} />);
    await screen.findByRole("tab", { name: /Everyone/ });
    expect(liveOnFix).toBeTruthy();
    act(() =>
      liveOnFix?.({
        subject_id: "s-me",
        lat: 50.0,
        lon: 5.0,
        accuracy_m: 5,
        battery_pct: 90,
        captured_at: new Date().toISOString(),
      }),
    );
    await waitFor(() =>
      expect(lastState?.pins?.find((p) => p.subjectId === "s-me")?.lat).toBe(50.0),
    );
  });

  it("extends the focused person's trail on a live fix", async () => {
    render(<MemberDashboard deps={deps()} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    await waitFor(() => expect(lastState?.fixes?.length).toBe(2)); // the fetched trail
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
    // The live fix for the focused subject appends to the trail.
    await waitFor(() => expect(lastState?.fixes?.length).toBe(3));
  });

  it("clears the trail when its fetch fails", async () => {
    const listPositions = vi.fn(async (): Promise<LocationFix[]> => {
      throw new Error("offline");
    });
    render(<MemberDashboard deps={deps({ listPositions })} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    await waitFor(() => expect(listPositions).toHaveBeenCalled());
    await waitFor(() => expect(lastState?.fixes?.length).toBe(0));
  });

  it("shows an empty-timeline note when expanded with no crossings", async () => {
    render(<MemberDashboard deps={deps({ listTimeline: vi.fn(async () => []) })} />);
    fireEvent.click(await screen.findByRole("tab", { name: /Bob/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Bob/ }));
    expect(await screen.findByText(/no arrivals or departures yet/i)).toBeInTheDocument();
  });
});
