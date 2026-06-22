import { describe, expect, it } from "vitest";
import { TRAVELING_MIN_MPS, speedColor, travelingSpeedMph } from "./speed";

describe("travelingSpeedMph", () => {
  it("hides speed when not traveling (null or below the cutoff)", () => {
    expect(travelingSpeedMph(null)).toBeNull();
    expect(travelingSpeedMph(0)).toBeNull();
    expect(travelingSpeedMph(TRAVELING_MIN_MPS - 0.01)).toBeNull();
  });

  it("formats mph above the cutoff", () => {
    expect(travelingSpeedMph(13.4)).toBe("30 mph"); // ~30 mph
    expect(travelingSpeedMph(4.4704)).toBe("10 mph"); // exactly 10 mph
  });
});

describe("speedColor", () => {
  it("returns an rgb() that ramps from slow to fast", () => {
    const slow = speedColor(0);
    const fast = speedColor(40); // well past the ramp max → the hot end
    expect(slow).toMatch(/^rgb\(/);
    expect(fast).toMatch(/^rgb\(/);
    expect(slow).not.toBe(fast);
  });

  it("treats a null speed as the slow end", () => {
    expect(speedColor(null)).toBe(speedColor(0));
  });
});
