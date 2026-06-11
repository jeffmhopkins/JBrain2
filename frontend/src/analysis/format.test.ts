import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { fmtTemporal } from "./format";

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
