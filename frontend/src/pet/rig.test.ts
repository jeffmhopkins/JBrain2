import { describe, expect, it } from "vitest";
import { ACTIONS, figureFor, rigFor } from "./rig";

describe("idle", () => {
  // "When robots stop moving, they look dead." A perfectly still frame is a regression.
  it("never rests the limbs perfectly still", () => {
    const a = rigFor(null, 0, 1, 0);
    const b = rigFor(null, 0, 1, 700);
    expect(a.armL).not.toBe(b.armL);
  });

  it("leaves the hands down when nothing is playing", () => {
    expect(rigFor(null, 0, 1, 0).handsUp).toBe(0);
  });
});

describe("peekaboo", () => {
  // The fix for a shipped dud: on the wall `hide` renders identically to `sit`. If handsUp
  // ever stops reaching 1 the gag silently becomes "arms up beside the head" again.
  it("raises the hands fully through the middle of the action", () => {
    expect(rigFor("hide", 0, 1, 0).handsUp).toBe(0);
    expect(rigFor("hide", 0.2, 1, 0).handsUp).toBe(1);
    expect(rigFor("hide", 0.5, 1, 0).handsUp).toBe(1);
    expect(rigFor("hide", 1, 1, 0).handsUp).toBe(0);
  });

  it("ramps up and back down rather than snapping", () => {
    const rising = rigFor("hide", 0.09, 1, 0).handsUp;
    const falling = rigFor("hide", 0.86, 1, 0).handsUp;
    expect(rising).toBeGreaterThan(0);
    expect(rising).toBeLessThan(1);
    expect(falling).toBeGreaterThan(0);
    expect(falling).toBeLessThan(1);
  });
});

describe("gag structure", () => {
  // Anticipation → a short act → a LONG HOLD → settle. The hold is the punchline, so it must
  // be the longest phase; a gag that snaps back is not funny to a four-year-old.
  it("holds the bewildered pose for about half the gag", () => {
    const held = [0.3, 0.4, 0.5, 0.6, 0.7].filter(
      (p) => figureFor("fart", p, 1, 0, 0, 0).extra === "puff",
    );
    expect(held).toHaveLength(5);
    expect(figureFor("fart", 0.05, 1, 0, 0, 0).extra).toBe("");
    expect(figureFor("fart", 0.9, 1, 0, 0, 0).extra).toBe("");
  });

  it("wears the bewildered face, never a pained or happy one", () => {
    expect(ACTIONS.fart?.face).toBe("bewildered");
    expect(ACTIONS.burp?.face).toBe("bewildered");
  });
});

describe("the repetition penalty reaches the body", () => {
  it("moves less when the magnitude is lower", () => {
    const full = rigFor("dance", 0.25, 1, 0);
    const damped = rigFor("dance", 0.25, 0.35, 0);
    expect(Math.abs(damped.armL - 40)).toBeLessThan(Math.abs(full.armL - 40));
  });
});

describe("figure transform", () => {
  it("keeps the jump lift bounded — the panel clips above ~32 px", () => {
    let peak = 0;
    for (let p = 0; p <= 1; p += 0.02) peak = Math.min(peak, figureFor("jump", p, 1, 0, 0, 0).oy);
    expect(peak).toBeGreaterThanOrEqual(-32);
  });

  it("breathes even with no action", () => {
    const a = figureFor(null, 0, 1, 0, 0, 0);
    const b = figureFor(null, 0, 1, 2200, 0, 0);
    expect(a.sy).not.toBe(b.sy);
  });

  it("passes the emotion's tilt and the rig's lean through", () => {
    expect(figureFor(null, 0, 1, 0, -7, 8).ang).toBe(1);
  });

  it("ignores an action it does not know instead of throwing", () => {
    expect(() => figureFor("teleport", 0.5, 1, 0, 0, 0)).not.toThrow();
    expect(rigFor("teleport", 0.5, 1, 0).handsUp).toBe(0);
  });
});
