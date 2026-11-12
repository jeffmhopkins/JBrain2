import { describe, expect, it } from "vitest";
import { byDay, dayHeading } from "./growbot";

// Local-time instants, so the day boundaries hold in any time zone the tests run in.
const NOW = new Date(2026, 10, 12, 9, 30);
const at = (d: number, h: number) => new Date(2026, 10, d, h, 0).toISOString();

describe("dayHeading", () => {
  it("names today and yesterday by local day, and older days by date", () => {
    expect(dayHeading(at(12, 0), NOW)).toBe("Today");
    expect(dayHeading(at(11, 23), NOW)).toBe("Yesterday");
    expect(dayHeading(at(10, 12), NOW)).toBe("10 Nov");
  });

  it("names the year of an older year's day, so the same date a year apart never merges", () => {
    const lastYear = new Date(2025, 10, 12, 9, 0).toISOString();
    expect(dayHeading(lastYear, NOW)).toBe("12 Nov 2025");
    const rows = [at(12, 8), { created_at: lastYear }].map((r) =>
      typeof r === "string" ? { created_at: r } : r,
    );
    expect(byDay(rows, NOW).map((g) => g.heading)).toEqual(["Today", "12 Nov 2025"]);
  });
});

describe("byDay", () => {
  it("keeps the order given and starts a group at each new day", () => {
    const rows = [at(12, 8), at(12, 7), at(11, 20), at(10, 14)].map((created_at) => ({
      created_at,
    }));
    expect(byDay(rows, NOW).map((g) => [g.heading, g.rows.length])).toEqual([
      ["Today", 2],
      ["Yesterday", 1],
      ["10 Nov", 1],
    ]);
    expect(byDay([], NOW)).toEqual([]);
  });
});
