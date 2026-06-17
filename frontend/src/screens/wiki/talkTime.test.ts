import { describe, expect, it } from "vitest";
import { talkTime } from "./talkTime";

describe("talkTime", () => {
  const now = new Date("2026-06-17T12:00:00Z");

  it("shows the clock for today", () => {
    // 09:14 local-of-the-test-runner; assert the 'today' prefix + minute padding.
    const label = talkTime("2026-06-17T09:14:00", now);
    expect(label.startsWith("today ")).toBe(true);
    expect(label).toMatch(/today \d{1,2}:\d{2}$/);
  });

  it("shows month + day within the year, and adds the year when older", () => {
    expect(talkTime("2026-03-12T10:00:00", now)).toBe("Mar 12");
    expect(talkTime("2025-12-01T10:00:00", now)).toBe("Dec 1, 2025");
  });

  it("returns empty for an unparseable date", () => {
    expect(talkTime("not-a-date", now)).toBe("");
  });
});
