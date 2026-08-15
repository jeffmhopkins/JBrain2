import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  currentTokenRate,
  endTurnRate,
  recordStreamedText,
  subscribeTokenRate,
} from "./tokenMeter";

describe("tokenMeter", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    // The meter is module state shared across tests — settle it before the next one.
    endTurnRate();
    vi.useRealTimers();
  });

  /** Stream `chars` characters every 100ms for `ms`, as a model actually would. */
  function stream(ms: number, charsPer100ms: number): void {
    for (let elapsed = 0; elapsed < ms; elapsed += 100) {
      recordStreamedText("x".repeat(charsPer100ms));
      vi.advanceTimersByTime(100);
    }
  }

  it("publishes a rate on the first delta rather than a second later", () => {
    const seen: (number | null)[] = [];
    subscribeTokenRate((r) => seen.push(r));

    recordStreamedText("hello there");

    expect(seen).toHaveLength(1);
    expect(seen[0]).toBeGreaterThan(0);
  });

  it("measures a steady stream at the rate it actually ran", () => {
    // 200 chars/sec ÷ 4 chars-per-token = 50 t/s.
    stream(4000, 20);
    expect(currentTokenRate()).toBeGreaterThanOrEqual(45);
    expect(currentTokenRate()).toBeLessThanOrEqual(55);
  });

  it("tracks a slowdown instead of averaging over the whole turn", () => {
    stream(4000, 40); // 100 t/s
    const fast = currentTokenRate() ?? 0;
    stream(4000, 10); // 25 t/s
    const slow = currentTokenRate() ?? 0;

    expect(fast).toBeGreaterThan(80);
    // The trailing window has fully turned over, so the old fast run is gone.
    expect(slow).toBeLessThan(40);
  });

  it("republishes once a second while a turn streams", () => {
    const seen: (number | null)[] = [];
    stream(1000, 20);
    subscribeTokenRate((r) => seen.push(r));

    vi.advanceTimersByTime(2000);

    expect(seen).toHaveLength(2);
  });

  it("drops to null after the stream goes quiet, then stops ticking", () => {
    const seen: (number | null)[] = [];
    stream(1000, 20);
    subscribeTokenRate((r) => seen.push(r));

    vi.advanceTimersByTime(10_000);

    expect(currentTokenRate()).toBeNull();
    expect(seen[seen.length - 1]).toBeNull();
    // Exactly one null: the timer is torn down once the reading lapses, so an idle
    // app isn't left running a forever-interval behind a top bar showing nothing.
    expect(seen.filter((r) => r === null)).toHaveLength(1);
  });

  it("does not average across a gap between tool calls", () => {
    stream(3000, 40); // a fast burst…
    vi.advanceTimersByTime(30_000); // …then a long tool call
    recordStreamedText("x".repeat(4));
    vi.advanceTimersByTime(1000);
    recordStreamedText("x".repeat(4));

    // The resumed reading reflects the trickle, not the burst 30 seconds ago.
    expect(currentTokenRate()).toBeLessThan(20);
  });

  it("clears immediately when the turn settles", () => {
    stream(2000, 20);
    expect(currentTokenRate()).not.toBeNull();

    endTurnRate();

    expect(currentTokenRate()).toBeNull();
  });

  it("stops delivering to an unsubscribed listener", () => {
    const seen: (number | null)[] = [];
    const off = subscribeTokenRate((r) => seen.push(r));
    recordStreamedText("hello");
    off();
    vi.advanceTimersByTime(2000);

    expect(seen).toHaveLength(1);
  });

  it("ignores an empty delta", () => {
    const seen: (number | null)[] = [];
    subscribeTokenRate((r) => seen.push(r));

    recordStreamedText("");

    expect(seen).toHaveLength(0);
    expect(currentTokenRate()).toBeNull();
  });
});
