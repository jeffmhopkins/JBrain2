import { describe, expect, it } from "vitest";
import { dayLabel, groupByDay, isWithinLastDays, relativeTime } from "./grouping";

// Local-time constructor keeps assertions timezone-independent.
const NOW = new Date(2026, 5, 10, 14, 30); // Wed Jun 10 2026, 14:30

describe("dayLabel", () => {
  it("labels today and yesterday", () => {
    expect(dayLabel(new Date(2026, 5, 10, 0, 1), NOW)).toBe("Today");
    expect(dayLabel(new Date(2026, 5, 9, 23, 59), NOW)).toBe("Yesterday");
  });

  it("labels older days with weekday + date", () => {
    expect(dayLabel(new Date(2026, 5, 8, 12, 0), NOW)).toBe("Mon, Jun 8");
  });

  it("includes the year for other years", () => {
    expect(dayLabel(new Date(2025, 11, 31, 12, 0), NOW)).toContain("2025");
  });
});

describe("groupByDay", () => {
  it("groups consecutive same-day items oldest-first", () => {
    const items = [
      { at: new Date(2026, 5, 8, 9, 0), id: "a" },
      { at: new Date(2026, 5, 9, 10, 0), id: "b" },
      { at: new Date(2026, 5, 9, 11, 0), id: "c" },
      { at: new Date(2026, 5, 10, 8, 0), id: "d" },
    ];
    const groups = groupByDay(items, (i) => i.at, NOW);
    expect(groups.map((g) => g.label)).toEqual(["Mon, Jun 8", "Yesterday", "Today"]);
    expect(groups[1]?.items.map((i) => i.id)).toEqual(["b", "c"]);
  });

  it("returns no groups for an empty stream", () => {
    expect(groupByDay([], () => NOW, NOW)).toEqual([]);
  });
});

describe("isWithinLastDays", () => {
  it("keeps today and yesterday for the 2-day home window", () => {
    expect(isWithinLastDays(new Date(2026, 5, 10, 0, 0), 2, NOW)).toBe(true);
    expect(isWithinLastDays(new Date(2026, 5, 9, 0, 0), 2, NOW)).toBe(true);
  });

  it("drops anything two calendar days back or older", () => {
    expect(isWithinLastDays(new Date(2026, 5, 8, 23, 59), 2, NOW)).toBe(false);
    expect(isWithinLastDays(new Date(2026, 4, 1), 2, NOW)).toBe(false);
  });

  it("keeps clock-skewed future timestamps (a pending send is always shown)", () => {
    expect(isWithinLastDays(new Date(2026, 5, 11, 1, 0), 2, NOW)).toBe(true);
  });
});

describe("relativeTime", () => {
  it("shows now / minutes / hours within 24h", () => {
    expect(relativeTime(new Date(NOW.getTime() - 30 * 1000), NOW)).toBe("now");
    expect(relativeTime(new Date(NOW.getTime() - 5 * 60 * 1000), NOW)).toBe("5m");
    expect(relativeTime(new Date(NOW.getTime() - 3 * 60 * 60 * 1000), NOW)).toBe("3h");
  });

  it("falls back to clock time beyond 24h", () => {
    const old = new Date(2026, 5, 7, 9, 14);
    expect(relativeTime(old, NOW)).toBe(
      old.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
    );
  });
});
