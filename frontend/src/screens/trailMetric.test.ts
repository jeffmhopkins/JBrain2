import { describe, expect, it } from "vitest";
import type { LocationFix } from "../api/client";
import { colorForFix, computeDwell, legendInfo, metricBucket, metricLabel } from "./trailMetric";

function fix(over: Partial<LocationFix> = {}): LocationFix {
  return {
    captured_at: "2026-06-22T12:00:00Z",
    latitude: 40,
    longitude: -74,
    accuracy_m: 8,
    battery_pct: 80,
    velocity_mps: null,
    course_deg: null,
    acceleration_mps2: null,
    altitude_m: null,
    ...over,
  };
}

describe("metricLabel", () => {
  it("formats each metric in its own units", () => {
    expect(metricLabel("speed", fix({ velocity_mps: 13.4 }))).toBe("30 mph");
    expect(metricLabel("accel", fix({ acceleration_mps2: 1.84 }))).toBe("1.8 m/s²");
    expect(metricLabel("battery", fix({ battery_pct: 64 }))).toBe("64%");
    expect(metricLabel("heading", fix({ course_deg: 47 }))).toBe("NE 47°");
    expect(metricLabel("timeplace", fix(), 8.6)).toBe("9 min");
  });

  it("reads — when the value is missing", () => {
    expect(metricLabel("speed", fix({ velocity_mps: null }))).toBe("—");
  });
});

describe("legendInfo", () => {
  it("uses a hue wheel + compass ticks for heading", () => {
    const h = legendInfo("heading", 0);
    expect(h.gradient).toMatch(/hsl/);
    expect(h.ticks).toEqual(["N", "E", "S", "W", "N"]);
  });

  it("labels time-at-place at the 5m/15m/1h breakpoints up to the window max", () => {
    expect(legendInfo("timeplace", 120).ticks).toEqual(["0", "5m", "15m", "1h", "2h"]);
    // A short window drops the breakpoints above its max.
    expect(legendInfo("timeplace", 12).ticks).toEqual(["0", "5m", "12m"]);
  });

  it("uses end ticks for the linear metrics", () => {
    expect(legendInfo("speed", 0).ticks).toEqual(["0", "60+"]);
  });
});

describe("time-at-place piecewise scale", () => {
  it("spreads short dwells across the ramp instead of squashing them at 0", () => {
    const f = fix();
    const max = 120; // a 2h window → anchors 0/5m/15m/1h/2h at 0,¼,½,¾,1
    const b = (min: number) => metricBucket("timeplace", f, min, max, 12);
    expect(b(5)).toBe(3); // 5m sits a quarter up the ramp, not ~0
    expect(b(15)).toBe(6);
    expect(b(60)).toBe(9);
    expect(b(120)).toBe(12);
  });
});

describe("colorForFix", () => {
  it("returns an rgb() for ramped metrics and hsl() for heading", () => {
    expect(colorForFix("speed", fix({ velocity_mps: 5 }), 0, 0)).toMatch(/^rgb\(/);
    expect(colorForFix("heading", fix({ course_deg: 120 }), 0, 0)).toMatch(/^hsl\(/);
  });
});

describe("computeDwell", () => {
  it("is empty for no fixes", () => {
    expect(computeDwell([])).toEqual({ dwell: [], max: 0 });
  });

  it("piles dwell onto a lingered-at cell and stays cool in transit", () => {
    // Two fixes far apart (transit), then three clustered at one spot 10 min apart.
    const fixes = [
      fix({ latitude: 40.0, longitude: -74.0, captured_at: "2026-06-22T12:00:00Z" }),
      fix({ latitude: 40.05, longitude: -74.05, captured_at: "2026-06-22T12:10:00Z" }),
      fix({ latitude: 40.1, longitude: -74.1, captured_at: "2026-06-22T12:20:00Z" }),
      fix({ latitude: 40.1, longitude: -74.1, captured_at: "2026-06-22T12:30:00Z" }),
      fix({ latitude: 40.1, longitude: -74.1, captured_at: "2026-06-22T12:40:00Z" }),
    ];
    const { dwell, max } = computeDwell(fixes);
    expect(dwell).toHaveLength(5);
    // The clustered location accrues more dwell than the transit points.
    const clustered = dwell[4] ?? 0;
    const transit = dwell[0] ?? 0;
    expect(clustered).toBeGreaterThan(transit);
    expect(max).toBe(Math.max(...dwell));
  });
});
