import { describe, expect, it } from "vitest";
import { buildProjector, heatCells } from "./locationMap";

describe("buildProjector", () => {
  it("projects the data centre to the viewport centre and north above it", () => {
    const south = { lat: 40.0, lon: -74.0 };
    const north = { lat: 40.01, lon: -74.0 };
    const { project } = buildProjector([south, north], 320, 320);
    const ps = project(south);
    const pn = project(north);
    // Same longitude → same x; both near the horizontal centre.
    expect(Math.round(ps.x)).toBe(Math.round(pn.x));
    expect(Math.abs(ps.x - 160)).toBeLessThan(1);
    // North has the higher latitude → smaller y (north is up).
    expect(pn.y).toBeLessThan(ps.y);
  });

  it("keeps a single point centred with a finite scale (span floor)", () => {
    const { project, metersPerPixel } = buildProjector([{ lat: 40, lon: -74 }], 320, 320);
    const p = project({ lat: 40, lon: -74 });
    expect(p).toEqual({ x: 160, y: 160 });
    expect(Number.isFinite(metersPerPixel)).toBe(true);
    expect(metersPerPixel).toBeGreaterThan(0);
  });

  it("scales a fence radius into pixels via metresPerPixel", () => {
    const { metersPerPixel } = buildProjector(
      [
        { lat: 40.0, lon: -74.0 },
        { lat: 40.01, lon: -74.01 },
      ],
      320,
      320,
    );
    expect(120 / metersPerPixel).toBeGreaterThan(0);
  });
});

describe("heatCells", () => {
  it("bins points and normalises weight to the busiest cell", () => {
    const cells = heatCells(
      [
        { x: 1, y: 1 },
        { x: 2, y: 2 }, // same 16px cell as (1,1) → count 2
        { x: 100, y: 100 }, // a different cell → count 1
      ],
      16,
    );
    expect(cells).toHaveLength(2);
    const weights = cells.map((c) => c.weight).sort();
    expect(weights[0]).toBeCloseTo(0.5);
    expect(weights[1]).toBe(1);
  });

  it("returns nothing for no points", () => {
    expect(heatCells([], 16)).toEqual([]);
  });
});
