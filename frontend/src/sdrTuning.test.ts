import { describe, expect, it } from "vitest";
import type { SpectrumRow } from "./sdrSpectrum";
import { CENTRED_BINS, CENTRED_SHARE, offsetLabel, spillLabel, tuningOf } from "./sdrTuning";

const TUNED_HZ = 146_940_000;
const BIN_HZ = 93.75; // 512 bins over a 48 kHz IF, as the sidecar sends it
const SPAN_HZ = 32_000; // twice a 16 kHz narrowband passband
const BINS = Math.round(SPAN_HZ / BIN_HZ);

/** A row with one flat-topped signal on a noise floor, offset from the tuned
 *  frequency by `offsetHz`. Flat-topped because that is what an FM carrier is, and
 *  the flat top is exactly what makes the argmax a bad centre estimate. */
function row(
  offsetHz: number,
  { widthHz = 14_000, peakDb = -34, floorDb = -78, passbandHz = 16_000 } = {},
): SpectrumRow {
  const startHz = TUNED_HZ - (BINS / 2) * BIN_HZ;
  const db: number[] = [];
  for (let i = 0; i < BINS; i += 1) {
    const hz = startHz + (i + 0.5) * BIN_HZ - TUNED_HZ;
    const away = Math.abs(hz - offsetHz);
    // A ripple on the floor and on the top, so nothing here passes by being perfectly
    // smooth — the flat top wanders, which is the jitter the midpoint rule exists for.
    const ripple = Math.sin(i * 1.7) * 0.8;
    db.push((away <= widthHz / 2 ? peakDb : floorDb) + ripple);
  }
  return {
    at: 0,
    startHz,
    stopHz: startHz + BINS * BIN_HZ,
    binHz: BIN_HZ,
    db,
    peaks: [],
    passbandHz,
    channelHz: 0,
  };
}

/** A wide-FM row as the sidecar really sends one: 512 bins over 240 kHz, a 180 kHz
 *  passband, and a station filling most of it. The narrowband `row` above cannot stand
 *  in for this — the whole difficulty is that the signal is most of the picture. */
function wideRow(offsetHz: number, { widthHz = 160_000, peakDb = -30, floorDb = -70 } = {}) {
  const bins = 512;
  const binHz = 468.75;
  const startHz = TUNED_HZ - (bins / 2) * binHz;
  const db: number[] = [];
  for (let i = 0; i < bins; i += 1) {
    const hz = startHz + (i + 0.5) * binHz - TUNED_HZ;
    const away = Math.abs(hz - offsetHz);
    db.push((away <= widthHz / 2 ? peakDb : floorDb) + Math.sin(i * 1.7) * 0.8);
  }
  return {
    at: 0,
    startHz,
    stopHz: startHz + bins * binHz,
    binHz,
    db,
    peaks: [],
    passbandHz: 180_000,
    channelHz: 0,
  } satisfies SpectrumRow;
}

describe("tuningOf", () => {
  it("finds a centred signal", () => {
    const found = tuningOf(row(0), TUNED_HZ);
    expect(found).not.toBeNull();
    expect(found?.centred).toBe(true);
    expect(found?.offsetHz).toBeCloseTo(0, -2);
  });

  it("measures how far off the signal is", () => {
    const found = tuningOf(row(6_200), TUNED_HZ);
    expect(found?.centred).toBe(false);
    expect(found?.offsetHz).toBeGreaterThan(5_800);
    expect(found?.offsetHz).toBeLessThan(6_600);
  });

  it("signs the offset the way the readout reads it", () => {
    expect(tuningOf(row(-4_000), TUNED_HZ)?.offsetHz).toBeLessThan(0);
    expect(tuningOf(row(4_000), TUNED_HZ)?.offsetHz).toBeGreaterThan(0);
  });

  it("says nothing about an empty channel", () => {
    // No signal at all: the loudest bin is wherever the noise happened to peak, and a
    // centre derived from that is a number with no content behind it.
    const quiet = row(0, { peakDb: -78 });
    expect(tuningOf(quiet, TUNED_HZ)).toBeNull();
  });

  it("ignores a row that is not a tuning view", () => {
    // The same stream carries band rows from a spectrum session. `passbandHz` is what
    // tells them apart, and a band row drawn as a channel would be centred on nothing.
    expect(tuningOf(row(0, { passbandHz: 0 }), TUNED_HZ)).toBeNull();
  });

  it("is not fooled by a flat top", () => {
    // The point of the midpoint rule. The argmax of a rippling plateau lands anywhere
    // across it; the midpoint of the shoulders does not move.
    const offsets = [0, 1, 2, 3, 4].map((n) =>
      tuningOf(row(0, { widthHz: 14_000 + n * 20 }), TUNED_HZ),
    );
    const values = offsets.map((o) => o?.offsetHz ?? Number.NaN);
    expect(Math.max(...values) - Math.min(...values)).toBeLessThan(BIN_HZ * 2);
  });

  it("reports how much of the signal is outside the passband", () => {
    expect(tuningOf(row(0), TUNED_HZ)?.spilled).toBeCloseTo(0, 2);
    // 14 kHz wide, centred 6.2 kHz high, against a +/-8 kHz passband: the part above
    // 8 kHz is outside, which is a bit over a third of it.
    const spilled = tuningOf(row(6_200), TUNED_HZ)?.spilled ?? 0;
    expect(spilled).toBeGreaterThan(0.25);
    expect(spilled).toBeLessThan(0.5);
  });

  it("keeps a second station out of the first one's width", () => {
    // Walking out from the peak rather than scanning the whole row: two signals swept
    // into one would put the midpoint in the empty space between them.
    const near = row(0);
    const startHz = near.startHz;
    for (let i = 0; i < near.db.length; i += 1) {
      const hz = startHz + (i + 0.5) * BIN_HZ - TUNED_HZ;
      if (Math.abs(hz - 13_000) <= 1_500) near.db[i] = -40;
    }
    expect(tuningOf(near, TUNED_HZ)?.offsetHz).toBeCloseTo(0, -2);
  });

  it("finds a broadcast station that fills two thirds of its row", () => {
    // The floor cannot be the row's median here: a 180 kHz station in a 240 kHz view
    // is two thirds of the bins, so the median lands INSIDE the signal and the reading
    // came back null — "nothing in this channel" on a station 40 dB over the noise.
    const found = tuningOf(wideRow(0), TUNED_HZ);
    expect(found).not.toBeNull();
    expect(found?.centred).toBe(true);
  });

  it("scales the centred tolerance to the passband", () => {
    // MEASURED ON AIR: an FM signal's instantaneous spectrum is asymmetric because the
    // carrier is swinging, so on a 180 kHz broadcast station the loudest part of one
    // frame wanders tens of kHz. Tuned exactly, 104.1 read 39 kHz "high". One bin of
    // tolerance would have put a "Centre it" prompt on a station that is exactly right.
    expect(tuningOf(wideRow(6_000), TUNED_HZ)?.centred).toBe(true);
    expect(CENTRED_SHARE * 180_000).toBeGreaterThan(CENTRED_BINS * BIN_HZ);
  });

  it("still calls a real narrowband error off centre", () => {
    // The tolerance must not swallow what it exists to report: 5% of a 16 kHz
    // passband is 800 Hz, and 6.2 kHz off a 25 kHz channel is the case the strip was
    // built for.
    expect(tuningOf(row(6_200), TUNED_HZ)?.centred).toBe(false);
    expect(tuningOf(row(900), TUNED_HZ)?.centred).toBe(false);
  });

  it("calls anything inside one bin centred", () => {
    const barely = tuningOf(row(BIN_HZ * CENTRED_BINS * 0.5), TUNED_HZ);
    expect(barely?.centred).toBe(true);
  });
});

describe("labels", () => {
  it("says on centre without a number", () => {
    expect(offsetLabel({ centred: true } as never)).toBe("On centre");
  });

  it("gives a tenth of a kHz up close and none far out", () => {
    expect(offsetLabel({ centred: false, offsetHz: 6_200 } as never)).toBe("6.2 kHz high");
    expect(offsetLabel({ centred: false, offsetHz: -12_000 } as never)).toBe("12 kHz low");
  });

  it("only mentions the passband when a real part of the signal is outside it", () => {
    expect(spillLabel({ spilled: 0.02 } as never)).toBe("");
    expect(spillLabel({ spilled: 0.33 } as never)).toContain("a third");
    expect(spillLabel({ spilled: 0.5 } as never)).toContain("half");
  });
});
