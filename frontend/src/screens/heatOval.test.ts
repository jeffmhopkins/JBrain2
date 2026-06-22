import { describe, expect, it } from "vitest";
import { type HeatFix, type LatLon, fixHeadingRad, heatOvalPoints } from "./heatOval";

const at = (lat: number, lon: number): LatLon => ({ lat, lon });
const moving = (v: number | null, course: number | null): HeatFix => ({
  velocity_mps: v,
  course_deg: course,
});

// A representative on-screen spot radius in meters (≈ heatRadius px at a city zoom).
const R = 20;

describe("heatOvalPoints", () => {
  it("leaves a near-stationary fix as a single round blob", () => {
    const pts = heatOvalPoints([moving(0.5, 90)], [at(40, -74)], 0.4, R);
    expect(pts).toEqual([[40, -74, 0.4]]);
  });

  it("smears a moving fix into a full-weight capsule along its heading (oval)", () => {
    // Heading due east (90°): the smear shifts longitude, not latitude.
    const pts = heatOvalPoints([moving(10, 90)], [at(40, -74)], 0.4, R);
    expect(pts.length).toBeGreaterThan(3); // the fix + a line of satellites
    const [center, ...sats] = pts;
    expect(center).toEqual([40, -74, 0.4]);
    for (const [lat, lon, w] of sats) {
      expect(lat).toBeCloseTo(40, 6); // due-east stretch keeps latitude fixed
      expect(lon).not.toBeCloseTo(-74, 6);
      expect(w).toBe(0.4); // full weight, so the capsule reads uniform (not a round core)
    }
    // Symmetric fore/aft about the fix.
    const lons = sats.map((p) => p[1]).sort((a, b) => a - b);
    expect((lons[0] as number) + (lons[lons.length - 1] as number)).toBeCloseTo(2 * -74, 6);
  });

  it("elongates even at low speed (sized against the spot radius)", () => {
    // Just over the moving threshold: still a clearly elongated oval, not a dot.
    const pts = heatOvalPoints([moving(1.5, 0)], [at(40, -74)], 0.4, R);
    const reachLat = Math.max(...pts.slice(1).map((p) => Math.abs(p[0] - 40)));
    // At least ~1.5 radii of stretch (minReach floor), well above a single radius.
    expect(reachLat).toBeGreaterThan((R * 1.5) / 111_320 - 1e-9);
  });

  it("caps the stretch so highway speed doesn't streak across the map", () => {
    const fast = heatOvalPoints([moving(200, 0)], [at(40, -74)], 0.4, R); // 200 m/s, due north
    const reachLat = Math.max(...fast.slice(1).map((p) => Math.abs(p[0] - 40)));
    // Capped at 220 m; never the un-capped 200*6 s of travel.
    expect(reachLat).toBeLessThanOrEqual(220 / 111_320 + 1e-9);
    expect(reachLat).toBeGreaterThan(150 / 111_320); // but still a long oval
  });
});

describe("fixHeadingRad", () => {
  it("uses the reported course when present", () => {
    expect(fixHeadingRad([moving(5, 90)], [at(40, -74)], 0)).toBeCloseTo(Math.PI / 2, 6);
  });

  it("falls back to the bearing from the previous point", () => {
    // Step due north: bearing 0.
    const h = fixHeadingRad([moving(5, null), moving(5, null)], [at(40, -74), at(40.1, -74)], 1);
    expect(h).toBeCloseTo(0, 6);
  });

  it("returns null with neither course nor a usable prior step", () => {
    expect(fixHeadingRad([moving(5, null)], [at(40, -74)], 0)).toBeNull();
    expect(
      fixHeadingRad([moving(5, null), moving(5, null)], [at(40, -74), at(40, -74)], 1),
    ).toBeNull();
  });
});
