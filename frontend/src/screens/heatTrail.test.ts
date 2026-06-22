import { describe, expect, it } from "vitest";
import { type LatLon, heatTrailPoints } from "./heatTrail";

const at = (lat: number, lon: number): LatLon => ({ lat, lon });
// Degrees of latitude for a given distance north (≈ 111.32 km per degree).
const deg = (meters: number) => meters / 111_320;

describe("heatTrailPoints", () => {
  it("returns just the fixes when there's no travel between them", () => {
    // Two fixes ~5 m apart: parked jitter, below the travel threshold — no fill.
    const pts = heatTrailPoints([at(40, -74), at(40 + deg(5), -74)], 0.4, 10);
    expect(pts).toEqual([
      [40, -74, 0.4],
      [40 + deg(5), -74, 0.4],
    ]);
  });

  it("fills a travelled segment with interpolated points (a ribbon)", () => {
    // 100 m leg, fill every 10 m → 9 interior points between the two fixes.
    const a = at(40, -74);
    const b = at(40 + deg(100), -74);
    const pts = heatTrailPoints([a, b], 0.4, 10);
    expect(pts).toHaveLength(2 + 9);
    // All collinear (same longitude) and full weight.
    for (const [, lon, w] of pts) {
      expect(lon).toBeCloseTo(-74, 9);
      expect(w).toBe(0.4);
    }
    // Strictly increasing latitude from a to b: a continuous ribbon, not a star.
    const lats = pts.map((p) => p[0]);
    for (let i = 1; i < lats.length; i++) {
      expect(lats[i] as number).toBeGreaterThan(lats[i - 1] as number);
    }
  });

  it("does not bridge a gap that's too long to be one leg", () => {
    // 1 km jump (offline/parked between samples): keep the endpoints, draw no line.
    const pts = heatTrailPoints([at(40, -74), at(40 + deg(1000), -74)], 0.4, 10);
    expect(pts).toHaveLength(2);
  });

  it("leaves a lone fix as a single blob", () => {
    expect(heatTrailPoints([at(40, -74)], 0.4, 10)).toEqual([[40, -74, 0.4]]);
  });

  it("keeps a stop round while filling the legs around it", () => {
    // Drive in, sit (two near-identical fixes), drive out: only the two moving legs fill.
    const pts = heatTrailPoints(
      [at(40, -74), at(40 + deg(80), -74), at(40 + deg(82), -74), at(40 + deg(162), -74)],
      0.4,
      20,
    );
    // 4 fixes + interior fill on the two 80 m legs (3 each at 20 m spacing), none on the
    // 2 m stop segment.
    expect(pts).toHaveLength(4 + 3 + 3);
  });
});
