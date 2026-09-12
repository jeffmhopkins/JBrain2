import { describe, expect, it } from "vitest";
import type { SpectrumRow } from "./sdrSpectrum";
import {
  CENTRED_BINS,
  CENTRED_SHARE,
  offsetLabel,
  passbandEdges,
  spillLabel,
  tuningOf,
} from "./sdrTuning";

const TUNED_HZ = 146_940_000;
const BIN_HZ = 93.75; // 512 bins over a 48 kHz IF, as the sidecar sends it
const SPAN_HZ = 32_000; // twice a 16 kHz narrowband passband
const BINS = Math.round(SPAN_HZ / BIN_HZ);

/** A row with one flat-topped signal on a noise floor, offset from the tuned
 *  frequency by `offsetHz`. Flat-topped because that is what an FM carrier is, and
 *  the flat top is exactly what makes the argmax a bad centre estimate. */
function row(
  offsetHz: number,
  { widthHz = 14_000, peakDb = -34, floorDb = -78, passbandHz = 16_000, passbandCentreHz = 0 } = {},
): SpectrumRow {
  const startHz = TUNED_HZ - (BINS / 2) * BIN_HZ;
  const db: number[] = [];
  for (let i = 0; i < BINS; i += 1) {
    const hz = startHz + i * BIN_HZ - TUNED_HZ;
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
    passbandCentreHz,
    channelHz: 0,
    gainDb: null,
    tunerBypassed: false,
    view: "channel" as const,
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
    const hz = startHz + i * binHz - TUNED_HZ;
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
    passbandCentreHz: 0,
    channelHz: 0,
    gainDb: null,
    tunerBypassed: false,
    view: "channel" as const,
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
      const hz = startHz + i * BIN_HZ - TUNED_HZ;
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
    expect(offsetLabel({ centred: false, errorHz: 6_200 } as never)).toBe("6.2 kHz high");
    expect(offsetLabel({ centred: false, errorHz: -12_000 } as never)).toBe("12 kHz low");
  });

  it("only mentions the passband when a real part of the signal is outside it", () => {
    expect(spillLabel({ spilled: 0.02 } as never)).toBe("");
    expect(spillLabel({ spilled: 0.33 } as never)).toContain("a third");
    expect(spillLabel({ spilled: 0.5 } as never)).toContain("half");
  });
});

describe("a one-sided passband (C14)", () => {
  // `usb` hears +300..+3400 Hz of the dial and `lsb` hears -3400..-300, so the passband
  // is 3100 Hz wide with its middle 1850 Hz off the dial. Everything here reads the same
  // for a symmetric mode, where the centre is zero.
  const USB = { passbandHz: 3_100, passbandCentreHz: 1_850, widthHz: 2_600 };
  const LSB = { passbandHz: 3_100, passbandCentreHz: -1_850, widthHz: 2_600 };

  it("puts the edges where the demodulator listens, not around the dial", () => {
    expect(passbandEdges(row(0, USB))).toEqual({ lowHz: 300, highHz: 3_400 });
    expect(passbandEdges(row(0, LSB))).toEqual({ lowHz: -3_400, highHz: -300 });
    // The symmetric case is untouched, which is what makes this safe everywhere else.
    expect(passbandEdges(row(0))).toEqual({ lowHz: -8_000, highHz: 8_000 });
  });

  it("calls a signal sitting in the passband CENTRED, though it is off the dial", () => {
    // The bug in one line. A correctly tuned USB signal sits at +1850 Hz from the dial,
    // and the strip told the owner to move a radio that was already right.
    const tuning = tuningOf(row(1_850, USB), TUNED_HZ);

    expect(tuning).not.toBeNull();
    expect(tuning?.centred).toBe(true);
    expect(tuning?.errorHz).toBeCloseTo(0, -2);
    // ...while `offsetHz` still says where it IS, which is what the marker points at.
    expect(tuning?.offsetHz).toBeCloseTo(1_850, -2);
    expect(offsetLabel(tuning as never)).toBe("On centre");
  });

  it("measures the error toward the passband, so the tune button lands the signal", () => {
    // A kilohertz low of where USB wants it. The correction is -1000, NOT the -2850 the
    // dial-relative offset would have asked for — which would have moved the signal to
    // the dial, outside the +300..+3400 the demodulator hears.
    const tuning = tuningOf(row(850, USB), TUNED_HZ);

    expect(tuning?.centred).toBe(false);
    expect(tuning?.errorHz).toBeCloseTo(-1_000, -2);
    expect(offsetLabel(tuning as never)).toBe("1.0 kHz low");
  });

  it("counts what spills out of the SIDEBAND, not out of a symmetric box", () => {
    // Sitting on the dial is the worst place for a USB signal: almost all of it is in
    // the sideband the back end rejects. Against the old symmetric ±1550 box it looked
    // perfectly placed.
    const wrong = tuningOf(row(0, USB), TUNED_HZ);
    const right = tuningOf(row(1_850, USB), TUNED_HZ);

    expect(wrong?.spilled).toBeGreaterThan(0.4);
    expect(right?.spilled).toBeLessThan(0.05);
    expect(spillLabel(wrong as never)).not.toBe("");
  });
});

describe("the bin-to-hertz convention (C20)", () => {
  // `startHz` is bin 0's CENTRE. The sidecar's `iq.Spectrometer.start_hz` is
  // `center_hz - (n // 2) * bin_hz`, and `fftshift` puts DC in bin `n / 2` — so
  // `startHz + i * binHz` addresses bin `i` exactly, which is what `Spectrum.stop_hz`'s
  // own comment says and what `peaks.py` does. This file added half a bin, so the
  // readout and the box's own peak frequencies sat on grids 46.9 Hz apart.
  it("puts a signal centred on the dial at zero offset, not half a bin off", () => {
    const centred = tuningOf(row(0), TUNED_HZ);

    expect(centred?.offsetHz).toBe(0);
    expect(centred?.errorHz).toBe(0);
  });

  it("reads a whole number of bins as a whole number of bins", () => {
    // Four bins high is 375 Hz at this raster, not 375 + 46.875.
    const off = tuningOf(row(4 * BIN_HZ), TUNED_HZ);

    expect(off?.offsetHz).toBeCloseTo(4 * BIN_HZ, 6);
  });
});

describe("a neighbour on the raster (C21)", () => {
  /** An FM channel row: the tuned station, plus one 200 kHz away whose skirt reaches
   *  into the outer third of the picture. `row()` above is narrowband, so this builds a
   *  broadcast-shaped one directly. */
  function dial({ tuned = -60, neighbour = -30 } = {}): SpectrumRow {
    const binHz = 937.5;
    const bins = 768; // 720 kHz — four times a ±90 kHz passband
    const startHz = TUNED_HZ - (bins / 2) * binHz;
    const db: number[] = [];
    for (let i = 0; i < bins; i += 1) {
      const hz = (i + 0.5 - bins / 2) * binHz;
      const inTuned = Math.abs(hz) <= 60_000;
      // The neighbour is centred 200 kHz up — outside the row — but 180 kHz wide, so
      // its lower skirt lands from +110 kHz to the row's edge.
      const inNeighbour = hz >= 110_000;
      db.push(inTuned ? tuned : inNeighbour ? neighbour : -80 + Math.sin(i) * 0.5);
    }
    return {
      at: 0,
      startHz,
      stopHz: startHz + bins * binHz,
      binHz,
      db,
      peaks: [],
      passbandHz: 180_000,
      passbandCentreHz: 0,
      channelHz: 200_000,
      gainDb: null,
      tunerBypassed: false,
      view: "channel" as const,
    };
  }

  it("reads the station being demodulated, not the louder one next door", () => {
    // MEASURED ON AIR beside a carrier at 96.494 MHz: tuning 96.3 put the row's
    // strongest bin at +157,969 Hz and 96.7 at -161,250 Hz — the same neighbour both
    // times, 158 kHz outside a 90 kHz passband.
    const tuning = tuningOf(dial(), TUNED_HZ);

    expect(tuning).not.toBeNull();
    // Centred on the dial, not 130 kHz up where the neighbour is.
    expect(Math.abs(tuning?.offsetHz ?? 1e9)).toBeLessThan(5_000);
    expect(tuning?.centred).toBe(true);
  });

  it("takes the noise floor from noise, not from the neighbour's skirt", () => {
    // The neighbour fills part of the ring the floor is measured in. A median can be
    // dragged onto it; a low quartile stays in the noise below.
    const crowded = tuningOf(dial(), TUNED_HZ);
    const empty = tuningOf(dial({ neighbour: -80 }), TUNED_HZ);

    expect(crowded).not.toBeNull();
    expect(empty).not.toBeNull();
    // Within a decibel of each other: the neighbour must not move the reading.
    expect(Math.abs((crowded?.overDb ?? 0) - (empty?.overDb ?? 0))).toBeLessThan(1);
  });
});
