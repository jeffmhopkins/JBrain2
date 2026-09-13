import { describe, expect, it } from "vitest";
import { EMOTIONS, type FaceKey, approach, isFaceKey, resolveFace } from "./face";

/** The whole reason this module exists: on the wall, `curious` and `sleepy` are pixel-identical
 *  apart from a chest colour, and `happy` and `excited` differ by 0.06 of a mouth bar. A face
 *  that repeats itself is the defect being fixed, so it is the first thing asserted. */
describe("emotions are distinguishable", () => {
  it("gives every emotion a distinct parameter set", () => {
    const seen = new Map<string, string>();
    for (const key of EMOTIONS) {
      const f = resolveFace(key);
      const sig = JSON.stringify([f.l, f.r, f.face, f.mouth]);
      const clash = seen.get(sig);
      expect(clash, `${key} renders identically to ${clash}`).toBeUndefined();
      seen.set(sig, key);
    }
    expect(seen.size).toBe(EMOTIONS.length);
  });

  it("separates curious from sleepy by more than one field", () => {
    const a = resolveFace("curious");
    const b = resolveFace("sleepy");
    const differing = (["uy", "ua", "ly", "lb", "sx", "sy"] as const).filter(
      (k) => a.l[k] !== b.l[k],
    );
    expect(differing.length).toBeGreaterThan(1);
  });

  it("separates happy from excited in shape, not only in timing", () => {
    const happy = resolveFace("happy");
    const excited = resolveFace("excited");
    expect(excited.rate).toBeGreaterThan(happy.rate);
    expect(excited.l.sy).not.toBe(happy.l.sy);
  });
});

describe("eye asymmetry", () => {
  it("mirrors the upper-lid angle for symmetric emotions", () => {
    const f = resolveFace("happy");
    expect(f.r.ua).toBe(-f.l.ua);
    expect(f.r.ly).toBe(f.l.ly);
  });

  // Asymmetry IS the signal for these two — if a refactor accidentally mirrors them, the
  // emotion silently stops reading and nothing else would catch it.
  it.each(["curious", "silly"] as FaceKey[])("keeps %s asymmetric", (key) => {
    const f = resolveFace(key);
    expect(JSON.stringify(f.l)).not.toBe(JSON.stringify(f.r));
  });
});

describe("isFaceKey", () => {
  it("accepts the shipped emotions and the gag face", () => {
    for (const key of EMOTIONS) expect(isFaceKey(key)).toBe(true);
    expect(isFaceKey("bewildered")).toBe(true);
  });

  // A server enum can grow. Blanking the pet on an unknown value would be the worst failure
  // mode available, so callers fall back — and that depends on this returning false, not throwing.
  it("rejects unknown, null and undefined without throwing", () => {
    expect(isFaceKey("ecstatic")).toBe(false);
    expect(isFaceKey(null)).toBe(false);
    expect(isFaceKey(undefined)).toBe(false);
  });
});

describe("approach", () => {
  it("halves the remaining distance at rate 1", () => {
    expect(approach(0, 1, 1)).toBeCloseTo(0.5);
    expect(approach(0.5, 1, 1)).toBeCloseTo(0.75);
  });

  it("never overshoots, however high the rate", () => {
    expect(approach(0, 1, 99)).toBeLessThanOrEqual(1);
    expect(approach(1, 0, 99)).toBeGreaterThanOrEqual(0);
  });

  it("reaches ~90% within four frames at rate 1 (the 66 ms figure at 50 fps)", () => {
    let v = 0;
    for (let i = 0; i < 4; i++) v = approach(v, 1, 1);
    expect(v).toBeGreaterThan(0.9);
  });
});
