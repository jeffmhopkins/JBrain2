import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { DeviceSummary, LocationFix, PlaceGeofence, TimelineEntry } from "../api/client";
import { placeNoteBody } from "./LocationMapTab";
import { type LocationDeps, LocationScreen, relativeTime, sentence } from "./LocationScreen";

// Leaflet needs a real browser layout engine; stub the map glue so the screen's
// React behavior (controls, data fetch, overlays-data) is what's under test. The
// shared `update` spy lets a test assert the state handed to the map.
const { mapUpdate } = vi.hoisted(() => ({ mapUpdate: vi.fn() }));
vi.mock("./leafletMap", () => ({
  createLocationMap: () => ({
    update: mapUpdate,
    centerOn: vi.fn(),
    setScheme: vi.fn(),
    destroy: vi.fn(),
  }),
  readTileScheme: () => "dark",
  writeTileScheme: vi.fn(),
}));

beforeEach(() => mapUpdate.mockClear());

function device(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return {
    id: "d1",
    label: "Jeff's phone",
    created_at: "2026-06-01T00:00:00+00:00",
    revoked: false,
    last_seen: new Date(Date.now() - 5 * 60_000).toISOString(),
    battery_pct: 72,
    connection: "wifi",
    velocity_mps: null,
    fix_count: 140,
    ...over,
  };
}

function entry(over: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    occurred_at: new Date(Date.now() - 35 * 60_000).toISOString(),
    subject_id: "d1",
    transition: "exit",
    place_entity_id: "p1",
    place_name: "Office",
    ...over,
  };
}

function fix(over: Partial<LocationFix> = {}): LocationFix {
  return {
    captured_at: new Date(Date.now() - 60_000).toISOString(),
    latitude: 40.0,
    longitude: -74.0,
    accuracy_m: 8,
    battery_pct: 80,
    velocity_mps: null,
    course_deg: null,
    acceleration_mps2: null,
    altitude_m: null,
    ...over,
  };
}

function place(over: Partial<PlaceGeofence> = {}): PlaceGeofence {
  return {
    place_entity_id: "p1",
    name: "Office",
    enabled: true,
    center: { lat: 40.0, lon: -74.0 },
    radius_m: 120,
    polygon: null,
    ...over,
  };
}

function deps(over: Partial<LocationDeps> = {}): LocationDeps {
  return {
    listDevices: vi.fn(async () => [device()]),
    mintPairingCode: vi.fn(async () => ({
      code: "CODE-1",
      expires_at: "2026-06-20T13:00:00Z",
      payload: "cGF5bG9hZA",
    })),
    renameDevice: vi.fn(async () => {}),
    revokeDevice: vi.fn(async () => {}),
    deleteDevice: vi.fn(async () => {}),
    listTimeline: vi.fn(async () => [entry()]),
    listPlaces: vi.fn(async () => [place()]),
    listFixes: vi.fn(async () => [fix({ latitude: 40.0 }), fix({ latitude: 40.001 })]),
    filePlaceNote: vi.fn(async () => {}),
    reverseGeocode: vi.fn(async () => "12 Market St, Springfield"),
    loadDigest: vi.fn(async () => ({
      period: "week",
      since: "2026-06-11T12:00:00Z",
      until: "2026-06-18T12:00:00Z",
      timezone: "UTC",
      days: [],
      nights_home: 0,
      nights_total: 7,
      places_visited: 0,
      longest_trip: null,
      seen: [],
      computed_at: "2026-06-18T12:00:00Z",
    })),
    ...over,
  };
}

describe("LocationScreen", () => {
  it("lands on the Map, then shows a phone with its status on the Phones tab", async () => {
    render(<LocationScreen deps={deps()} />);
    // The map is the landing tab.
    await screen.findByLabelText("Location map");
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    await screen.findByText("Jeff's phone");
    const meta = screen.getByText(/last seen/);
    expect(meta.textContent).toMatch(/72% battery/);
    expect(meta.textContent).toMatch(/wifi/);
    expect(meta.textContent).toMatch(/140 fixes/);
  });

  it("shows speed in mph when the phone is traveling, hides it when still", async () => {
    const traveling = device({ label: "Moving phone", velocity_mps: 13.4 }); // ~30 mph
    const still = device({ id: "d2", label: "Parked phone", velocity_mps: 0.3 }); // below cutoff
    render(<LocationScreen deps={deps({ listDevices: vi.fn(async () => [traveling, still]) })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    const moving = await screen.findByText("Moving phone");
    expect(moving.closest("button")?.textContent).toMatch(/30 mph/);
    const parked = screen.getByText("Parked phone");
    expect(parked.closest("button")?.textContent).not.toMatch(/mph/);
  });

  it("shows the empty state when there are no devices", async () => {
    render(<LocationScreen deps={deps({ listDevices: vi.fn(async () => []) })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    expect(await screen.findByText(/no phones yet/)).toBeInTheDocument();
  });

  it("draws the map for the selected device's fixes and date range", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    // The map fetches fixes for the device over the default window and renders.
    expect(await screen.findByLabelText("Location map")).toBeInTheDocument();
    await waitFor(() => expect(d.listFixes).toHaveBeenCalled());
    const call = (d.listFixes as ReturnType<typeof vi.fn>).mock.calls[0] ?? [];
    const [subjectId, since, until] = call;
    expect(subjectId).toBe("d1");
    expect(since < until).toBe(true);
  });

  it("refetches when the map mode and dates change", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    await screen.findByLabelText("Location map");
    // Switching modes is client-only (no refetch); changing the window refetches.
    fireEvent.click(screen.getByRole("tab", { name: "Heat" }));
    const before = (d.listFixes as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.change(screen.getByLabelText("From date"), {
      target: { value: "2026-01-01T08:30" },
    });
    await waitFor(() =>
      expect((d.listFixes as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("shows the no-fixes note when the range is empty", async () => {
    const d = deps({ listFixes: vi.fn(async () => []) });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    expect(await screen.findByText(/no fixes in this range/i)).toBeInTheDocument();
  });

  it("shows the heat spot-size control only in Heat mode and feeds it to the map", async () => {
    render(<LocationScreen deps={deps()} />);
    await screen.findByLabelText("Location map");
    // The control is Heat-only.
    expect(screen.queryByLabelText("Heat spot size")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Heat" }));
    const slider = screen.getByLabelText("Heat spot size") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "40" } });
    expect(slider.value).toBe("40");
    // The chosen radius reaches the map glue.
    await waitFor(() => {
      const last = mapUpdate.mock.calls.at(-1)?.[0];
      expect(last).toMatchObject({ mode: "heat", heatRadius: 40 });
    });
  });

  it("captions the map with the latest fix's on-box address", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    expect(await screen.findByText(/12 Market St, Springfield/)).toBeInTheDocument();
    // Reverse-geocodes the newest fix (last in the oldest-first list).
    await waitFor(() => expect(d.reverseGeocode).toHaveBeenCalledWith(40.001, -74.0));
  });

  it("files a place note from the geofence editor", async () => {
    const d = deps({ listPlaces: vi.fn(async () => []) });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    fireEvent.click(await screen.findByRole("button", { name: "Places" }));
    fireEvent.click(await screen.findByRole("button", { name: "＋ Add place" }));

    fireEvent.change(screen.getByLabelText("Place name"), { target: { value: "Home" } });
    fireEvent.change(screen.getByLabelText("Latitude"), { target: { value: "40" } });
    fireEvent.change(screen.getByLabelText("Longitude"), { target: { value: "-74" } });
    fireEvent.change(screen.getByLabelText("Radius (meters)"), { target: { value: "150" } });
    fireEvent.click(screen.getByRole("button", { name: "File place note" }));

    await waitFor(() =>
      expect(d.filePlaceNote).toHaveBeenCalledWith({
        name: "Home",
        lat: 40,
        lon: -74,
        radiusM: 150,
      }),
    );
    // The confirmation makes the note-sourced flow explicit.
    expect(await screen.findByText(/filed as a place note/i)).toBeInTheDocument();
  });

  it("prefills the editor when editing an existing place", async () => {
    const d = deps({
      listPlaces: vi.fn(async () => [
        place({ name: "Office", center: { lat: 41, lon: -75 }, radius_m: 120 }),
      ]),
    });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Map" }));
    fireEvent.click(await screen.findByRole("button", { name: /Places/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Office/ }));
    expect((screen.getByLabelText("Place name") as HTMLInputElement).value).toBe("Office");
    expect((screen.getByLabelText("Latitude") as HTMLInputElement).value).toBe("41");
    expect((screen.getByLabelText("Radius (meters)") as HTMLInputElement).value).toBe("120");
  });

  it("renders the timeline as natural sentences naming the device", async () => {
    const d = deps({
      listTimeline: vi.fn(async () => [
        entry({ subject_id: "d1", transition: "exit", place_name: "Office" }),
        entry({ subject_id: "d2", transition: "enter", place_name: "Mom's house" }),
      ]),
      listDevices: vi.fn(async () => [
        device({ id: "d1", label: "Jeff's phone" }),
        device({ id: "d2", label: "Celine's phone" }),
      ]),
    });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(await screen.findByText("Jeff's phone left Office")).toBeInTheDocument();
    expect(screen.getByText("Celine's phone arrived at Mom's house")).toBeInTheDocument();
  });

  it("shows the timeline empty state when there are no crossings", async () => {
    const d = deps({ listTimeline: vi.fn(async () => []) });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    expect(await screen.findByText(/no movement yet/i)).toBeInTheDocument();
  });

  it("mints a pairing code for a NEW phone and shows the payload to scan or copy", async () => {
    const d = deps({ listDevices: vi.fn(async () => []) });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    await screen.findByText("＋ Pair a phone");
    fireEvent.click(screen.getByText("＋ Pair a phone"));

    fireEvent.change(screen.getByLabelText("Phone name"), { target: { value: "Jeff's phone" } });
    fireEvent.click(screen.getByRole("button", { name: "Create code" }));

    await waitFor(() => expect(d.mintPairingCode).toHaveBeenCalledWith("Jeff's phone"));
    // The self-contained payload is shown for the app to scan/paste.
    expect(await screen.findByText("cGF5bG9hZA")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
  });

  // The swipe rail opens on a tap too (the non-gesture path), so tests drive it
  // by clicking the phone face, then the rail action.
  async function openRail(name = /Jeff's phone/): Promise<void> {
    fireEvent.click(await screen.findByRole("button", { name }));
  }

  it("re-pairs an existing phone — mints a code BOUND to it and shows the QR", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    await openRail();
    fireEvent.click(screen.getByRole("button", { name: "re-pair" }));
    // The code is minted for THIS device id (redeeming it rotates the key).
    await waitFor(() => expect(d.mintPairingCode).toHaveBeenCalledWith("Jeff's phone", 1, "d1"));
    expect(await screen.findByText("cGF5bG9hZA")).toBeInTheDocument();
  });

  it("renames a phone inline from the rail", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    await openRail();
    fireEvent.click(screen.getByRole("button", { name: "rename" }));
    const input = screen.getByLabelText("Phone name");
    fireEvent.change(input, { target: { value: "Jeff's new phone" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(d.renameDevice).toHaveBeenCalledWith("d1", "Jeff's new phone"));
  });

  it("revokes a phone only after a tap-again confirm", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    await openRail();
    // First tap arms; nothing fires yet.
    fireEvent.click(screen.getByRole("button", { name: "revoke" }));
    expect(d.revokeDevice).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "tap again" }));
    await waitFor(() => expect(d.revokeDevice).toHaveBeenCalledWith("d1"));
  });

  it("deletes a phone only after a tap-again confirm", async () => {
    const d = deps();
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    await openRail();
    fireEvent.click(screen.getByRole("button", { name: "delete" }));
    expect(d.deleteDevice).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "tap again" }));
    await waitFor(() => expect(d.deleteDevice).toHaveBeenCalledWith("d1"));
  });

  it("filters revoked phones into their own segment with a restore action", async () => {
    const d = deps({
      listDevices: vi.fn(async () => [
        device({ id: "d1", label: "Jeff's phone" }),
        device({ id: "d2", label: "Old phone", revoked: true }),
      ]),
    });
    render(<LocationScreen deps={d} />);
    fireEvent.click(screen.getByRole("tab", { name: "Phones" }));
    // Active by default: the revoked phone is hidden.
    await screen.findByText("Jeff's phone");
    expect(screen.queryByText("Old phone")).toBeNull();
    // Switch to the Revoked segment — the revoked phone shows, restorable.
    fireEvent.click(screen.getByRole("tab", { name: /Revoked/ }));
    await screen.findByText("Old phone");
    fireEvent.click(screen.getByRole("button", { name: /Old phone/ }));
    expect(screen.getByRole("button", { name: "restore" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "revoke" })).toBeNull();
  });
});

describe("sentence", () => {
  const labels = new Map([["d1", "Jeff's phone"]]);
  it("maps a crossing to a plain verb sentence", () => {
    expect(sentence(entry({ transition: "exit", place_name: "Office" }), labels)).toBe(
      "Jeff's phone left Office",
    );
    expect(sentence(entry({ transition: "enter", place_name: "Home" }), labels)).toBe(
      "Jeff's phone arrived at Home",
    );
  });
  it("falls back to a generic who when the device is unknown", () => {
    expect(sentence(entry({ subject_id: "gone", transition: "enter" }), labels)).toBe(
      "A device arrived at Office",
    );
  });
});

describe("placeNoteBody", () => {
  it("states the circle geometry so the geofence predicate extracts cleanly", () => {
    const body = placeNoteBody({ name: "Home", lat: 40, lon: -74, radiusM: 150 });
    expect(body).toContain("Home");
    expect(body).toContain("150 meters");
    expect(body).toContain("latitude 40");
    expect(body).toContain("longitude -74");
  });
});

describe("relativeTime", () => {
  it("buckets recent timestamps", () => {
    expect(relativeTime(new Date(Date.now() - 30_000).toISOString())).toBe("just now");
    expect(relativeTime(new Date(Date.now() - 5 * 60_000).toISOString())).toBe("5m ago");
    expect(relativeTime(new Date(Date.now() - 3 * 3_600_000).toISOString())).toBe("3h ago");
    expect(relativeTime(new Date(Date.now() - 2 * 86_400_000).toISOString())).toBe("2d ago");
    expect(relativeTime("not-a-date")).toBe("unknown");
  });
});
