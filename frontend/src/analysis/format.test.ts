import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { FactOut } from "../api/client";
import { factValue, fmtQuantity, fmtTemporal } from "./format";

// The field bug only reproduces in a negative-offset zone: UTC-midnight
// calendar dates rendered locally slip to the previous evening. Node re-reads
// TZ at format time, so pinning it here makes the regression observable
// regardless of the CI host's zone.
beforeAll(() => {
  vi.stubEnv("TZ", "America/Denver");
});
afterAll(() => {
  vi.unstubAllEnvs();
});

describe("fmtTemporal", () => {
  it("day precision renders the stored UTC calendar day — no timezone shift", () => {
    expect(fmtTemporal("1986-03-19T00:00:00Z", "day")).toBe("Mar 19, 1986");
  });

  it("month precision renders the stored UTC month — March stays March", () => {
    expect(fmtTemporal("1986-03-01T00:00:00Z", "month")).toBe("Mar 1986");
  });

  it("year and era precision render the stored UTC year", () => {
    expect(fmtTemporal("1986-01-01T00:00:00Z", "year")).toBe("1986");
    expect(fmtTemporal("1986-01-01T00:00:00Z", "era")).toBe("1986");
  });

  it("unknown precision is treated as a calendar date, not an instant", () => {
    expect(fmtTemporal("1986-03-19T00:00:00Z", "unknown")).toBe("Mar 19, 1986");
  });

  it("instant precision keeps local rendering — a real moment in time", () => {
    // 03:00 UTC is the prior evening in Denver (UTC-6/-7): locals saw it then.
    expect(fmtTemporal("2026-06-10T03:00:00Z", "instant")).toBe("Jun 9, 2026");
  });

  it("null renders the em-dash placeholder", () => {
    expect(fmtTemporal(null, "day")).toBe("—");
  });
});

function fact(value_json: unknown): FactOut {
  return {
    id: "f1",
    entity_id: "e1",
    entity_name: "Jeff",
    predicate: "height",
    qualifier: null,
    kind: "attribute",
    statement: "Jeff is 6'4\" tall.",
    value_json,
    assertion: "asserted",
    status: "active",
    pinned: false,
    confidence: 0.9,
    valid_from: null,
    valid_to: null,
    reported_at: "2026-06-10T23:00:00-06:00",
    temporal_precision: "unknown",
    source_snippet: null,
  };
}

describe("fmtQuantity / factValue imperial display", () => {
  it("normalized inch lengths ≥ 24 read as feet'inches\"", () => {
    expect(fmtQuantity(76, "in")).toBe("6'4\"");
    expect(factValue(fact({ value: 76, unit: "in" }))).toBe("6'4\"");
  });

  it("short inch values stay in inches — parts, not people", () => {
    expect(fmtQuantity(23, "in")).toBe("23 in");
  });

  it("whole feet render a zero inch part", () => {
    expect(fmtQuantity(72, "in")).toBe("6'0\"");
  });

  it("non-inch units are untouched", () => {
    expect(factValue(fact({ value: 255, unit: "lb" }))).toBe("255 lb");
    expect(factValue(fact({ value: 193, unit: "cm" }))).toBe("193 cm");
  });

  it("blood pressure and statement fallback keep their rendering", () => {
    expect(factValue(fact({ systolic: 128, diastolic: 82, unit: "mmHg" }))).toBe("128/82 mmHg");
    expect(factValue(fact(null))).toBe("Jeff is 6'4\" tall.");
  });
});
