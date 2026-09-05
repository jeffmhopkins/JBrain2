/** Holding signals across rows: the question one row cannot answer.
 *
 *  The finding itself is the box's (`peaks.py`, tested there against fixtures). What is
 *  checked here is whether a marker on screen is the same signal it was a moment ago —
 *  which is the difference between a list an owner can read and one that flickers.
 */

import { describe, expect, it } from "vitest";
import {
  type HeldPeak,
  MAX_HELD,
  labelled,
  mergePeaks,
  positionOf,
  visiblePeaks,
} from "./sdrPeaks";
import type { SpectrumRow } from "./sdrSpectrum";

function row(peaks: { hz: number; db: number }[], at = 100, binHz = 9375): SpectrumRow {
  return {
    at,
    startHz: 88_000_000,
    stopHz: 108_000_000,
    binHz,
    db: [],
    passbandHz: 0,
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

  it("holds until it is cleared, however long the signal has been gone", () => {
    // No timer. A band watched for an hour should still be able to say what went
    // through it, and an expiry makes the answer depend on when you happened to look.
    const seen = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 100));

    const muchLater = mergePeaks(seen, row([], 100 + 3600));

    expect(muchLater).toHaveLength(1);
    expect(muchLater[0]).toMatchObject({ hz: 100_000_000, live: false });
  });

  it("keeps what it has seen when the clock goes backwards", () => {
    // A reconnect restarts the box clock. With no expiry there is no arithmetic on it
    // to go wrong, and the band check is what clears a picture that moved.
    const seen = mergePeaks([], row([{ hz: 100_000_000, db: -50 }], 500));

    expect(mergePeaks(seen, row([], 100))).toHaveLength(1);
  });

  it("is bounded, and drops the weakest when it has to be", () => {
    // Held has no timer, so this is the only bound — a wideband scan left all night
    // would otherwise accumulate without limit. The point of holding is remembering
    // what was there, so the loudest survive.
    let held = mergePeaks([], row([{ hz: 90_000_000, db: -10 }], 100));
    for (let n = 0; n < MAX_HELD + 20; n += 1) {
      held = mergePeaks(held, row([{ hz: 91_000_000 + n * 100_000, db: -80 }], 101 + n));
    }

    expect(held).toHaveLength(MAX_HELD);
    expect(held[0]).toMatchObject({ hz: 90_000_000 });
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

describe("which markers carry their frequency", () => {
  const at = (hz: number, db: number, position: number) => ({
    peak: { hz, db },
    at: position,
  });

  it("drops a label that would sit on top of its neighbour", () => {
    // MEASURED by the owner on a city FM dial: fourteen stations across 20 MHz put
    // labels on top of each other and the middle read as `99 30001309106 923 66204.105…`
    // — text that is worse than none, because it looks like a measurement.
    const named = labelled([
      at(100_000_000, -40, 0.5),
      at(100_100_000, -50, 0.52),
      at(100_200_000, -60, 0.54),
    ]);

    expect(named.size).toBe(1);
    expect(named.has(100_000_000)).toBe(true);
  });

  it("keeps the strongest when a stretch is crowded, not the leftmost", () => {
    // What a glance should read is the loudest thing in that part of the band.
    const named = labelled([at(100_000_000, -70, 0.5), at(100_100_000, -30, 0.52)]);

    expect([...named]).toEqual([100_100_000]);
  });

  it("labels everything when there is room", () => {
    const named = labelled([at(100_000_000, -40, 0.1), at(105_000_000, -50, 0.9)]);

    expect(named.size).toBe(2);
  });
});
