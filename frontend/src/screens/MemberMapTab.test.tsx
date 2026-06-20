import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LocationFix, MemberSubject, PlaceGeofence } from "../api/client";
import type { MemberDeps } from "./MemberDashboard";
import { MemberMapTab } from "./MemberMapTab";

// Leaflet needs real layout; mock the glue and capture its update() calls.
const { mapUpdate } = vi.hoisted(() => ({ mapUpdate: vi.fn() }));
vi.mock("./leafletMap", () => ({
  createLocationMap: () => ({ update: mapUpdate, destroy: vi.fn() }),
}));

// Capture the live-socket handler so a test can push a fix through it.
const { liveCb, liveClose } = vi.hoisted(() => ({
  liveCb: { current: null as null | ((f: unknown) => void) },
  liveClose: vi.fn(),
}));
vi.mock("./liveSocket", () => ({
  connectLive: (onFix: (f: unknown) => void) => {
    liveCb.current = onFix;
    return { close: liveClose };
  },
}));

function subject(over: Partial<MemberSubject> = {}): MemberSubject {
  return {
    subject_id: "s1",
    label: "Bob",
    last_seen: null,
    battery_pct: null,
    connection: null,
    ...over,
  };
}

function fix(over: Partial<LocationFix> = {}): LocationFix {
  return {
    captured_at: new Date().toISOString(),
    latitude: 40,
    longitude: -74,
    accuracy_m: 10,
    battery_pct: 80,
    ...over,
  };
}

function place(): PlaceGeofence {
  return {
    place_entity_id: "e1",
    name: "Home",
    enabled: true,
    center: { lat: 40, lon: -74 },
    radius_m: 120,
    polygon: null,
  };
}

function deps(over: Partial<MemberDeps> = {}): MemberDeps {
  return {
    probe: vi.fn(),
    listRoster: vi.fn(async () => [subject()]),
    listTimeline: vi.fn(),
    listPositions: vi.fn(async () => [fix()]),
    listPlaces: vi.fn(async () => [place()]),
    ...over,
  };
}

describe("MemberMapTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    liveCb.current = null;
  });

  it("draws the selected subject's trail with the shared fences", async () => {
    render(<MemberMapTab deps={deps()} />);
    await screen.findByLabelText("Family map");
    await waitFor(() => {
      const last = mapUpdate.mock.calls.at(-1)?.[0];
      expect(last.fixes).toHaveLength(1);
      expect(last.places).toHaveLength(1);
      expect(last.mode).toBe("trail");
    });
  });

  it("extends the trail when a live fix arrives for the selected subject", async () => {
    render(<MemberMapTab deps={deps()} />);
    await waitFor(() => expect(liveCb.current).not.toBeNull());
    await waitFor(() => expect(mapUpdate.mock.calls.at(-1)?.[0].fixes).toHaveLength(1));

    liveCb.current?.({
      subject_id: "s1",
      lat: 41,
      lon: -75,
      accuracy_m: 8,
      battery_pct: 79,
      captured_at: new Date().toISOString(),
    });
    await waitFor(() => expect(mapUpdate.mock.calls.at(-1)?.[0].fixes).toHaveLength(2));

    // A fix for a different subject is ignored (the map shows one person).
    liveCb.current?.({
      subject_id: "other",
      lat: 0,
      lon: 0,
      accuracy_m: null,
      battery_pct: null,
      captured_at: new Date().toISOString(),
    });
    // Still 2 — the foreign fix did not extend the trail.
    expect(mapUpdate.mock.calls.at(-1)?.[0].fixes).toHaveLength(2);
  });

  it("shows a person picker only when more than one subject is visible", async () => {
    const { rerender } = render(<MemberMapTab deps={deps()} />);
    await screen.findByLabelText("Family map");
    expect(screen.queryByLabelText("Person")).not.toBeInTheDocument();

    rerender(
      <MemberMapTab
        deps={deps({
          listRoster: vi.fn(async () => [subject(), subject({ subject_id: "s2", label: "Cara" })]),
        })}
      />,
    );
    await screen.findByLabelText("Person");
  });
});
