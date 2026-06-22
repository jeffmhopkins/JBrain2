import { describe, expect, it } from "vitest";
import { type HeatFix, type LatLon, fixHeadingRad, fixSpeedMps, heatOvalPoints } from "./heatOval";

const at = (lat: number, lon: number): LatLon => ({ lat, lon });
const moving = (v: number | null, course: number | null): HeatFix => ({
  velocity_mps: v,
  course_deg: course,
});

// A representative on-screen spot radius in metres (≈ heatRadius px at a city zoom).
const R = 20;
const reachLatOf = (pts: [number, number, number][], lat0: number) =>
  Math.max(...pts.slice(1).map((p) => Math.abs(p[0] - lat0)));

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

  it("scales the oval length with speed, in multiples of the spot radius", () => {
    const slow = reachLatOf(heatOvalPoints([moving(3, 0)], [at(40, -74)], 0.4, R), 40);
    const fast = reachLatOf(heatOvalPoints([moving(28, 0)], [at(40, -74)], 0.4, R), 40);
    // Highway is dramatically longer than a crawl — an unmistakable oval, not ~1.5:1.
    expect(fast).toBeGreaterThan(slow * 2);
    // Full-speed half-length ≈ 7.5 radii (1.5 + 6); the outermost point lands within one
    // step of that, so check the band rather than the exact value.
    expect(fast).toBeGreaterThan((R * 7) / 111_320);
    expect(fast).toBeLessThanOrEqual((R * 7.5) / 111_320 + 1e-9);
  });

  it("saturates the length at highway speed (no streak across the map)", () => {
    const fast = reachLatOf(heatOvalPoints([moving(28, 0)], [at(40, -74)], 0.4, R), 40);
    const faster = reachLatOf(heatOvalPoints([moving(80, 0)], [at(40, -74)], 0.4, R), 40);
    expect(faster).toBeCloseTo(fast, 6); // clamped at full-speed length
  });
});

describe("fixSpeedMps", () => {
  it("prefers the reported velocity", () => {
    expect(fixSpeedMps([moving(12, null)], [at(40, -74)], 0)).toBe(12);
  });

  it("estimates speed from neighbour spacing when velocity is missing", () => {
    // ~111.32 m due north between samples / 5 s ≈ 22.3 m/s.
    const v = fixSpeedMps(
      [moving(null, null), moving(null, null)],
      [at(40, -74), at(40.001, -74)],
      1,
    );
    expect(v).toBeCloseTo(111.32 / 5, 1);
  });

  it("is null with no velocity and no neighbour", () => {
    expect(fixSpeedMps([moving(null, null)], [at(40, -74)], 0)).toBeNull();
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
