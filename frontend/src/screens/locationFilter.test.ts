import { describe, expect, it } from "vitest";
import type { LocationFix } from "../api/client";
import { ACCURACY_GATE_M, withinAccuracy } from "./locationFilter";

function fix(over: Partial<LocationFix>): LocationFix {
  return {
    captured_at: "2026-06-21T00:00:00Z",
    latitude: 40,
    longitude: -74,
    accuracy_m: 10,
    battery_pct: 80,
    velocity_mps: null,
    ...over,
  };
}

describe("withinAccuracy", () => {
  it("drops fixes whose accuracy radius exceeds the gate", () => {
    const fixes = [fix({ accuracy_m: 5 }), fix({ accuracy_m: 500 }), fix({ accuracy_m: 50 })];
    expect(withinAccuracy(fixes).map((f) => f.accuracy_m)).toEqual([5, 50]);
  });

  it("keeps a null accuracy (unknown, not assumed bad)", () => {
    expect(withinAccuracy([fix({ accuracy_m: null })])).toHaveLength(1);
  });

  it("keeps a fix exactly at the gate and preserves order", () => {
    const fixes = [fix({ accuracy_m: ACCURACY_GATE_M }), fix({ accuracy_m: 1 })];
    expect(withinAccuracy(fixes).map((f) => f.accuracy_m)).toEqual([ACCURACY_GATE_M, 1]);
  });

  it("honors a custom gate", () => {
    const fixes = [fix({ accuracy_m: 30 }), fix({ accuracy_m: 80 })];
    expect(withinAccuracy(fixes, 50).map((f) => f.accuracy_m)).toEqual([30]);
  });
});
