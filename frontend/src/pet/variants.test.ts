import { describe, expect, it } from "vitest";
import {
  PENALTY_FLOOR,
  PENALTY_RESET_MS,
  POOLS,
  newMemory,
  pickVariant,
  repetitionPenalty,
} from "./variants";

/** A deterministic "random" so selection is assertable. */
const fixed = (v: number) => () => v;

describe("pickVariant", () => {
  it("honours a cooldown by not repeating a just-played variant", () => {
    const mem = newMemory();
    const first = pickVariant("poke", mem, 0, fixed(0));
    const second = pickVariant("poke", mem, 100, fixed(0));
    expect(second).not.toBe(first);
  });

  it("lets a variant back in once its cooldown has expired", () => {
    const mem = newMemory();
    const first = pickVariant("poke", mem, 0, fixed(0));
    const later = pickVariant("poke", mem, 60_000, fixed(0));
    expect(later).toBe(first);
  });

  // The one outcome a child must never get is nothing at all: a silent no-op reads as broken.
  // So an exhausted pool falls back to the full table rather than returning empty.
  it("still answers when every variant is cooling down", () => {
    const mem = newMemory();
    const all = POOLS.poke ?? [];
    for (const o of all) mem.lastPlayed[`poke:${o.v}`] = 1000;
    const picked = pickVariant("poke", mem, 1001, fixed(0.5));
    expect(all.map((o) => o.v)).toContain(picked);
  });

  it("returns the pool name for an unknown pool rather than throwing", () => {
    expect(pickVariant("nope", newMemory(), 0)).toBe("nope");
  });

  it("respects weights — the rare variant needs a high draw", () => {
    const mem = newMemory();
    expect(pickVariant("poke", mem, 0, fixed(0.01))).toBe("wiggle");
    expect(pickVariant("poke", newMemory(), 0, fixed(0.999))).toBe("hiccup");
  });

  it("only ever returns variants declared in the pool", () => {
    const mem = newMemory();
    const names = new Set((POOLS.poke ?? []).map((o) => o.v));
    for (let i = 0; i < 200; i++) {
      expect(names.has(pickVariant("poke", mem, i * 137))).toBe(true);
    }
  });
});

describe("repetitionPenalty", () => {
  it("is full strength the first time", () => {
    expect(repetitionPenalty("poke", newMemory(), 0)).toBe(1);
  });

  it("decays over a rapid burst", () => {
    const mem = newMemory();
    const first = repetitionPenalty("poke", mem, 0);
    const fifth = [1, 2, 3, 4].map((i) => repetitionPenalty("poke", mem, i * 200)).at(-1);
    expect(fifth).toBeLessThan(first);
  });

  // It must never reach zero: a motionless response is indistinguishable from a broken toy.
  it("never falls below the floor, however hard it is hammered", () => {
    const mem = newMemory();
    let last = 1;
    for (let i = 0; i < 100; i++) last = repetitionPenalty("poke", mem, i * 50);
    expect(last).toBe(PENALTY_FLOOR);
  });

  // Preschoolers love repetition — they will ask for the burp a hundred times. Recency is
  // suppressed WITHIN a gag, never applied to the gag itself, so a pause restores it fully.
  it("resets to full after a quiet gap", () => {
    const mem = newMemory();
    for (let i = 0; i < 10; i++) repetitionPenalty("poke", mem, i * 100);
    expect(repetitionPenalty("poke", mem, 10 * 100 + PENALTY_RESET_MS + 1)).toBe(1);
  });

  it("tracks pools independently", () => {
    const mem = newMemory();
    for (let i = 0; i < 5; i++) repetitionPenalty("poke", mem, i * 100);
    expect(repetitionPenalty("fart", mem, 600)).toBe(1);
  });
});
