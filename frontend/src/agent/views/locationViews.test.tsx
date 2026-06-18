import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ViewPayload } from "../types";
import { ToolView, isKnownView } from "./registry";

// Leaflet needs a real layout engine; mock the inline map glue so the view's
// React behavior (segments list, owner-gating, tap-to-expand) is what's under
// test, and the spies let us assert the gap-split legs / place center handed to
// the map — never rendered tiles (mirrors screens/LocationScreen.test.tsx:15).
const { trailSpy, placeSpy } = vi.hoisted(() => ({ trailSpy: vi.fn(), placeSpy: vi.fn() }));
vi.mock("./locationMap", () => ({
  renderTrail: (...args: unknown[]) => {
    trailSpy(...args);
    return { invalidate: vi.fn(), destroy: vi.fn() };
  },
  renderPlace: (...args: unknown[]) => {
    placeSpy(...args);
    return { invalidate: vi.fn(), destroy: vi.fn() };
  },
}));

beforeEach(() => {
  trailSpy.mockClear();
  placeSpy.mockClear();
});

interface SpyLeg {
  points: [number, number][];
  fix_count: number;
}
// noUncheckedIndexedAccess is on; these assert a call happened and return its
// typed args, so the body never indexes a possibly-undefined slot.
function legsOfCall(i: number): SpyLeg[] {
  const call = trailSpy.mock.calls[i];
  if (!call) throw new Error(`no renderTrail call #${i}`);
  return call[1] as SpyLeg[];
}
function interactiveOfCall(i: number): unknown {
  const call = trailSpy.mock.calls[i];
  if (!call) throw new Error(`no renderTrail call #${i}`);
  return call[2];
}
function leg(legs: SpyLeg[], i: number): SpyLeg {
  const l = legs[i];
  if (!l) throw new Error(`no leg #${i}`);
  return l;
}

function mapPayload(over: Partial<Record<string, unknown>> = {}): ViewPayload {
  return {
    view: "location_map",
    surface: "inline",
    data: {
      timezone: "America/Denver",
      total_fixes: 1914,
      total_distance_m: 38000,
      legs: [
        {
          points: [
            [40.019, -105.27],
            [40.028, -105.26],
          ],
          fix_count: 1180,
          started_at: "2026-06-09T07:50:00-06:00",
          ended_at: "2026-06-11T21:10:00-06:00",
          distance_m: 22000,
        },
        {
          points: [
            [40.07, -105.2],
            [40.019, -105.27],
          ],
          fix_count: 734,
          started_at: "2026-06-12T04:25:00-06:00",
          ended_at: "2026-06-14T18:40:00-06:00",
          distance_m: 16000,
        },
      ],
      gaps: [
        {
          after_leg: 0,
          started_at: "2026-06-11T21:10:00-06:00",
          ended_at: "2026-06-12T04:25:00-06:00",
          seconds: 25500,
        },
      ],
      ...over,
    },
    refs: [],
  };
}

function placePayload(over: Partial<Record<string, unknown>> = {}): ViewPayload {
  return {
    view: "place_card",
    surface: "inline",
    data: {
      name: "Home",
      address: "1412 Linden Ave, Boulder, CO",
      center: [40.019, -105.27],
      radius_m: 110,
      owner: true,
      stats: [
        { value: "41 min", label: "last seen" },
        { value: "214", label: "visits 90d" },
      ],
      chips: [
        { label: "Celine", kind: "person" },
        { label: "Maple", kind: "animal" },
      ],
      ...over,
    },
    refs: [],
  };
}

describe("ToolView registry", () => {
  it("renders nothing for an unknown view name", () => {
    expect(isKnownView("location_map")).toBe(true);
    expect(isKnownView("place_card")).toBe(true);
    expect(isKnownView("evil_iframe")).toBe(false);
    const { container } = render(
      <ToolView payload={{ view: "evil_iframe", surface: "inline", data: {}, refs: [] }} />,
    );
    // The wrapper isn't even emitted — an unknown view is rejected outright.
    expect(container.querySelector(".tool-view")).toBeNull();
  });
});

describe("location_map (Option B answer-first)", () => {
  it("hands the gap-split legs to the map and never bridges the gap", () => {
    render(<ToolView payload={mapPayload()} />);
    // The thumbnail draws once; the legs handed over are the two separate legs.
    expect(trailSpy).toHaveBeenCalledTimes(1);
    const legs = legsOfCall(0);
    expect(legs).toHaveLength(2);
    // Leg 2's first point is NOT leg 1's last point — the gap is not drawn across.
    const leg0 = leg(legs, 0);
    const leg1 = leg(legs, 1);
    expect(leg1.points[0]).not.toEqual(leg0.points[leg0.points.length - 1]);
  });

  it("spells out the gap as a text segments row, with no coordinate caption", () => {
    const { container } = render(<ToolView payload={mapPayload()} />);
    expect(screen.getByText("No signal — route unknown")).toBeTruthy();
    expect(screen.getByText(/2 legs/)).toBeTruthy();
    // No lat/lon leaks into the rendered text (coordinates stay inside the map).
    expect(container.textContent).not.toContain("40.0");
    expect(container.textContent).not.toContain("-105");
  });

  it("draws a second (interactive) map only after tap-to-expand", () => {
    render(<ToolView payload={mapPayload()} />);
    expect(trailSpy).toHaveBeenCalledTimes(1); // thumbnail only
    fireEvent.click(screen.getByLabelText("Expand map"));
    // The expanded map is interactive; the thumbnail was not.
    expect(trailSpy).toHaveBeenCalledTimes(2);
    expect(interactiveOfCall(1)).toEqual({ interactive: true });
    expect(interactiveOfCall(0)).toEqual({ interactive: false });
  });

  it("renders an empty placeholder (no map) when the window has no fixes", () => {
    render(<ToolView payload={mapPayload({ legs: [], gaps: [], total_fixes: 0 })} />);
    expect(screen.getByText(/No location in this window/)).toBeTruthy();
    expect(trailSpy).not.toHaveBeenCalled();
  });

  it("renders a single pin (where_is-style one-fix trail)", () => {
    const single = mapPayload({
      legs: [
        {
          points: [[40.019, -105.27]],
          fix_count: 1,
          started_at: "2026-06-14T18:40:00-06:00",
          ended_at: "2026-06-14T18:40:00-06:00",
          distance_m: 0,
        },
      ],
      gaps: [],
    });
    render(<ToolView payload={single} />);
    const legs = legsOfCall(0);
    expect(legs).toHaveLength(1);
    expect(leg(legs, 0).points).toHaveLength(1);
    expect(screen.getByText(/1 leg\b/)).toBeTruthy();
  });

  it("shows a stale freshness pill (last known, not here-now)", () => {
    render(
      <ToolView payload={mapPayload({ freshness: "stale", fresh_label: "last fix 3 h ago" })} />,
    );
    const pill = screen.getByText("last fix 3 h ago");
    expect(pill.className).toContain("stale");
  });

  it("offline window with no trail shows the placeholder, no map, no pill", () => {
    render(
      <ToolView
        payload={mapPayload({
          legs: [],
          gaps: [],
          total_fixes: 0,
          freshness: "offline",
          fresh_label: "no recent fix",
        })}
      />,
    );
    expect(screen.getByText(/No location in this window/)).toBeTruthy();
    expect(screen.queryByText("no recent fix")).toBeNull();
    expect(trailSpy).not.toHaveBeenCalled();
  });

  it("downsamples a long trail upstream — it draws exactly the points it is given", () => {
    // The view never re-thins; it draws the (already bounded) points from the
    // payload. A 3-point leg is handed through verbatim.
    const long = mapPayload({
      legs: [
        {
          points: [
            [40.0, -105.0],
            [40.01, -105.01],
            [40.02, -105.02],
          ],
          fix_count: 9999,
          started_at: "2026-06-09T07:50:00-06:00",
          ended_at: "2026-06-09T21:10:00-06:00",
          distance_m: 5000,
        },
      ],
      gaps: [],
    });
    render(<ToolView payload={long} />);
    const only = leg(legsOfCall(0), 0);
    expect(only.points).toHaveLength(3);
    expect(only.fix_count).toBe(9999); // the summary count is the full leg
  });
});

describe("place_card (owner-gated stats)", () => {
  it("shows the name, address, owner stats, and note-sourced chips", () => {
    render(<ToolView payload={placePayload()} />);
    expect(screen.getByText("Home")).toBeTruthy();
    expect(screen.getByText("1412 Linden Ave, Boulder, CO")).toBeTruthy();
    expect(screen.getByText("visits 90d")).toBeTruthy();
    expect(screen.getByText("owner-only stats")).toBeTruthy();
    expect(screen.getByText("Celine")).toBeTruthy();
    // The mini-map got the centre; no coordinate is in the rendered text.
    expect(placeSpy).toHaveBeenCalledWith(expect.anything(), [40.019, -105.27], 110);
  });

  it("omits the stat block for a non-owner payload", () => {
    render(<ToolView payload={placePayload({ owner: false })} />);
    expect(screen.getByText("Home")).toBeTruthy();
    expect(screen.queryByText("visits 90d")).toBeNull();
    expect(screen.queryByText("owner-only stats")).toBeNull();
  });

  it("never prints coordinates as text", () => {
    const { container } = render(<ToolView payload={placePayload()} />);
    expect(container.textContent).not.toContain("40.019");
    expect(container.textContent).not.toContain("-105.27");
  });
});
