/** Holding signals across rows: the question one row cannot answer.
 *
 *  The finding itself is the box's (`peaks.py`, tested there against fixtures). What is
 *  checked here is whether a marker on screen is the same signal it was a moment ago —
 *  which is the difference between a list an owner can read and one that flickers.
 */

import { describe, expect, it } from "vitest";
import { type HeldPeak, mergePeaks, positionOf, visiblePeaks } from "./sdrPeaks";
import type { SpectrumRow } from "./sdrSpectrum";

function row(peaks: { hz: number; db: number }[], at = 100, binHz = 9375): SpectrumRow {
  return {
    at,
    startHz: 88_000_000,
    stopHz: 108_000_000,
    binHz,
    db: [],
    peaks: peaks.map((p) => ({ ...p, overDb: 12 })),
  };
}

describe("holding a signal across rows", () => {
  it("a carrier that wanders a bin is the same signal, not a new one", () => {
    // Noise moves which bin of a carrier's own skirt is strongest, so an exact match
    // would report a new station on every row — the flicker this file exists to remove,
    // reintroduced by its own matcher.
    const first = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 100));

    const second = mergePeaks(first, row([{ hz: 100_009_375, db: -51 }], 101));

    expect(second).toHaveLength(1);
    expect(second[0]).toMatchObject({ hz: 100_009_375, db: -51, live: true });
  });

  it("keeps a signal that stopped, and marks it as remembered", () => {
    // A repeater keys up and drops. One marker that appears and goes, not forty.
    const seen = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 100));

    const later = mergePeaks(seen, row([], 105));

    expect(later).toHaveLength(1);
    expect(later[0]).toMatchObject({ hz: 100_000_000, live: false, seen: 100 });
  });

  it("forgets a signal once it has been gone long enough", () => {
    const seen = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 100));

    expect(mergePeaks(seen, row([], 100 + 21))).toEqual([]);
  });

  it("drops what it remembers when the clock goes backwards", () => {
    // A retune or a reconnect restarts the clock, and `now - seen` is then not a
    // duration at all — a held marker would otherwise sit there forever.
    const seen = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 500));

    expect(mergePeaks(seen, row([], 100))).toEqual([]);
  });

  it("does not let one held marker claim two real stations", () => {
    // Two carriers a few bins apart would otherwise take turns matching each other.
    const held = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 100));

    const both = mergePeaks(
      held,
      row(
        [
          { hz: 100_000_000, db: -50 },
          { hz: 100_009_000, db: -55 },
        ],
        101,
      ),
    );

    expect(both).toHaveLength(2);
    expect(both.every((p) => p.live)).toBe(true);
  });

  it("lists the strongest first, because that is what a glance wants", () => {
    const merged = mergePeaks(
      [],
      row(
        [
          { hz: 100_000_000, db: -70 },
          { hz: 102_000_000, db: -40 },
        ],
        100,
      ),
    );

    expect(merged.map((p) => p.db)).toEqual([-40, -70]);
  });
});

describe("what the toggle chooses", () => {
  const held: HeldPeak[] = [
    { hz: 100_000_000, db: -40, overDb: 20, seen: 100, live: true },
    { hz: 102_000_000, db: -60, overDb: 9, seen: 90, live: false },
  ];

  it("live shows only what the newest row found", () => {
    expect(visiblePeaks(held, "live").map((p) => p.hz)).toEqual([100_000_000]);
  });

  it("held shows what has been on the air recently, present or not", () => {
    expect(visiblePeaks(held, "held")).toHaveLength(2);
  });
});

describe("placing a marker on the picture", () => {
  it("puts a signal where the row says it is", () => {
    expect(positionOf(98_000_000, row([]))).toBeCloseTo(0.5, 6);
  });

  it("refuses a signal the row does not cover rather than pinning it to the edge", () => {
    // The band changes under this view on every retune, and a clamped marker claims a
    // signal at a frequency that was never measured.
    expect(positionOf(50_000_000, row([]))).toBeNull();
    expect(positionOf(200_000_000, row([]))).toBeNull();
  });
});
