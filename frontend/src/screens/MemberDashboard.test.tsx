import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MemberSubject, PlaceGeofence, Principal } from "../api/client";
import { MemberDashboard, type MemberDeps } from "./MemberDashboard";
import type { MapState } from "./leafletMap";

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
    ...over,
  };
}

beforeEach(() => {
  updateSpy.mockClear();
  lastState = null;
  onSelect = null;
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
});
