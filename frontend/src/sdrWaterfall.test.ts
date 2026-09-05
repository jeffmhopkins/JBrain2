// The half of a waterfall that can be wrong without anyone noticing.
//
// A colour scale a few dB out does not throw — it paints a busy band that looks empty,
// or a quiet one that looks alive. So the scale, the ramp and the bin-to-pixel mapping
// are checked here as arithmetic, where a wrong answer is a failure rather than a
// picture nobody questions.

import { describe, expect, it } from "vitest";
import type { SpectrumRow } from "./sdrSpectrum";
import {
  CALIBRATION_SECONDS,
  HISTORY_SECONDS,
  calibrate,
  calibrated,
  frameRate,
  historyRows,
  paint,
  reduce,
  rowPixelsFor,
  shade,
} from "./sdrWaterfall";

function row(db: number[], startHz = 144_000_000, binHz = 25_000): SpectrumRow {
  // No peaks: this file is about the COLOUR arithmetic, and a fixture that carried
  // findings would be asserting on a rule tested where it lives (test_sdr_peaks.py).
  return {
    at: 0,
    startHz,
    stopHz: startHz + db.length * binHz,
    binHz,
    db,
    peaks: [],
    passbandHz: 0,
  };
}

/** `count` rows a `gap` apart on the box clock, NEWEST FIRST — the order the waterfall
 *  holds its history in, and the order everything below reads it in. */
function stream(count: number, gap: number): SpectrumRow[] {
  return [...Array(count).keys()].map((i) => ({
    ...row([-70, -60]),
    at: 1000 - i * gap,
  }));
}

describe("the colour window", () => {
  it("starts just above the floor rather than at the lowest reading", () => {
    // The contrast-slider gesture: a palette anchored to the single quietest bin spends
    // most of its range on noise, and every real signal lands in the top few percent.
    const noisy = [...Array(100).keys()].map((i) => -70 + (i % 5));
    const scale = calibrate([row([...noisy, -120])]);

    expect(scale.lowDb).toBeGreaterThan(-80);
  });

  it("never collapses on a dead-flat band", () => {
    // A disconnected antenna reads the radio's own noise: almost no spread at all, and
    // a palette stretched across it turns rounding into a light show.
    const scale = calibrate([row(Array(64).fill(-52))]);

    expect(scale.highDb - scale.lowDb).toBeGreaterThanOrEqual(12);
  });

  it("answers something usable with no rows at all", () => {
    const scale = calibrate([]);

    expect(scale.highDb).toBeGreaterThan(scale.lowDb);
  });

  it("ignores readings that are not numbers", () => {
    // A frame can carry a bin rtl_power never wrote. Folding NaN into the percentile
    // would poison the whole window.
    const scale = calibrate([row([-70, Number.NaN, -30, -50])]);

    expect(Number.isFinite(scale.lowDb) && Number.isFinite(scale.highDb)).toBe(true);
  });

  it("is taken from a handful of rows, not one", () => {
    // One row of a bursty band is whatever happened in that second.
    expect(calibrated(stream(1, 1))).toBe(false);
  });
});

describe("how fast the rows are coming", () => {
  it("says nothing until it has seen enough gaps to mean it", () => {
    // Sizing a three-minute canvas off one gap would let a single late frame decide how
    // much history the picture keeps.
    expect(frameRate(stream(3, 0.1))).toBeNull();
  });

  it("reads the rtl_power tier and the I/Q tier off the same rows", () => {
    expect(frameRate(stream(8, 1))).toBeCloseTo(1);
    expect(frameRate(stream(8, 0.1))).toBeCloseTo(10);
  });

  it("survives one stalled frame, because it takes the median gap", () => {
    // A retune barrier or a reconnect leaves a single huge gap. An average over it would
    // halve the history the picture keeps for the next three minutes.
    const rows = stream(8, 0.1);
    (rows[3] as SpectrumRow).at -= 5;
    for (let i = 4; i < rows.length; i += 1) (rows[i] as SpectrumRow).at -= 5;

    expect(frameRate(rows)).toBeCloseTo(10);
  });

  it("refuses to time a stream whose rows carry no clock", () => {
    // `at` defaults to 0 on a row that did not carry one. Zero gaps are not an infinitely
    // fast stream, and reading them as one would ask for an unbounded canvas.
    expect(frameRate(stream(8, 0))).toBeNull();
  });
});

describe("how much history is three minutes", () => {
  it("is the same 180 rows it always was on a one-a-second stream", () => {
    expect(historyRows(1, 512)).toBe(HISTORY_SECONDS);
  });

  it("is ten times that when the rows come ten times as fast", () => {
    // The bug this replaces: a constant 180 rows meant eighteen seconds at 10 fps, while
    // the comment beside it still said three minutes.
    expect(historyRows(10, 512)).toBe(HISTORY_SECONDS * 10);
  });

  it("assumes the slow tier until a rate is known", () => {
    // The first rows arrive before any gap has been measured. Guessing fast would
    // allocate a canvas ten times too tall for a stream that never fills it.
    expect(historyRows(null, 512)).toBe(HISTORY_SECONDS);
  });

  it("will not size a canvas off a clock that glitched", () => {
    expect(historyRows(1000, 512)).toBe(HISTORY_SECONDS * 10);
  });

  it("spends history rather than asking for a canvas the browser refuses", () => {
    // Finer bins (plan F7) multiply into the same pixel budget as more rows do.
    const wide = historyRows(10, 4096);
    const wider = historyRows(10, 1_000_000);

    expect(wide).toBe(HISTORY_SECONDS * 10); // 4096 x 1800 fits, so nothing is given up
    expect(wider).toBeLessThan(wide);
    expect(wider).toBeGreaterThan(0);
  });
});

describe("holding the colour window", () => {
  it("is not frozen off eight tenths of a second", () => {
    // The bug this replaces: eight ROWS is eight seconds at 1 fps and 0.8 s at 10 fps —
    // well inside the settling window after a retune. Freeze a bad window there and the
    // whole session is painted wrong, silently.
    expect(calibrated(stream(8, 0.1))).toBe(false);
  });

  it("is frozen once the rows cover eight seconds, at either rate", () => {
    expect(calibrated(stream(9, 1))).toBe(true);
    expect(calibrated(stream(81, 0.1))).toBe(true);
    expect(calibrated(stream(80, 0.1))).toBe(false);
  });

  it("still wants a handful of rows however long they took", () => {
    // Two rows a minute apart span the window and are still two readings.
    expect(calibrated(stream(2, 60))).toBe(false);
  });

  it("falls back to counting rows when the stream carries no clock", () => {
    // Otherwise the window is re-taken forever and the picture renormalises around
    // whatever is on the air — the failure the freeze exists to prevent.
    expect(calibrated(stream(8, 0))).toBe(true);
  });

  it("holds the window for a whole calibration window, not a token one", () => {
    expect(CALIBRATION_SECONDS).toBeGreaterThanOrEqual(8);
  });
});

describe("the ramp", () => {
  it("runs dark blue to amber, the same as the still image of a sweep", () => {
    const scale = { lowDb: -80, highDb: -20 };

    expect(shade(-80, scale)).toEqual([14, 15, 24]);
    expect(shade(-50, scale)).toEqual([54, 105, 154]);
    expect(shade(-20, scale)).toEqual([254, 205, 94]);
  });

  it("clamps rather than running off either end", () => {
    const scale = { lowDb: -80, highDb: -20 };

    expect(shade(-200, scale)).toEqual(shade(-80, scale));
    expect(shade(0, scale)).toEqual(shade(-20, scale));
  });

  it("draws a missing reading as nothing, not as a floor", () => {
    expect(shade(Number.NaN, { lowDb: -80, highDb: -20 })).toEqual([0, 0, 0]);
  });
});

describe("painting a picture", () => {
  const scale = { lowDb: -80, highDb: -20 };

  it("puts the newest row at the bottom", () => {
    // The owner's call (2026-09-04): the live edge sits against the frequency axis it
    // is measured on, and history rises away from it.
    const pixels = paint([row([-20]), row([-80])], 1, 2, scale);

    expect([pixels[4], pixels[5], pixels[6]]).toEqual([254, 205, 94]);
    expect([pixels[0], pixels[1], pixels[2]]).toEqual([14, 15, 24]);
  });

  it("leaves rows it does not have yet transparent, ABOVE the live edge", () => {
    // So a picture that is still filling reads as empty rather than as a band with
    // nothing on it — two very different claims about the radio. And the blank half has
    // to be the OLD half: flipping the finished picture instead of indexing from the
    // bottom would park the emptiness over the newest rows.
    const pixels = paint([row([-40])], 1, 3, scale);

    expect(pixels[11]).toBe(255); // the newest row, at the bottom
    expect(pixels[7]).toBe(0);
    expect(pixels[3]).toBe(0);
  });

  it("leaves the tail of a short row transparent rather than filling it", () => {
    // A frame that lost a block is a missing measurement, not a quiet one. Painting the
    // floor colour there would claim a reading that was never taken.
    // `extent` is the band's full width; the row carries only half of it. Without that
    // the two bins it does have would be stretched across the whole picture.
    const pixels = paint([row([-40, -40])], 4, 1, scale, 4);

    expect(pixels[3]).toBe(255);
    expect(pixels[7]).toBe(255);
    expect(pixels[11]).toBe(0);
    expect(pixels[15]).toBe(0);
  });

  it("fills exactly the buffer an ImageData of that size wants", () => {
    expect(paint([row([-40, -50, -60])], 3, 4, scale).length).toBe(3 * 4 * 4);
  });
});

describe("reduce", () => {
  it("gives a column the same value however the picture is scrolled", () => {
    // The twinkle, stated as a test. A column is a pure function of the bins under it,
    // so nothing about when or how often it is drawn can change what it shows —
    // which is what a browser resampler could not promise, because its answer depended
    // on a filter phase that moved with the ring's write head.
    const db = Array.from({ length: 4096 }, (_, i) => -60 + (i % 7));

    const once = reduce(db, 800);
    const again = reduce(db, 800);

    expect(Array.from(again)).toEqual(Array.from(once));
  });

  it("keeps a carrier that is narrower than a pixel", () => {
    // Max-hold rather than mean, and this is why: at five bins a pixel a mean puts a
    // real transmitter four fifths of the way back into the noise, so the one thing
    // the owner is looking for is the thing the averaging removes.
    const db = new Array(4096).fill(-90);
    db[2001] = -20;

    const columns = reduce(db, 800);

    expect(Math.max(...columns)).toBe(-20);
  });

  it("leaves no column empty when there are more pixels than bins", () => {
    // A narrow band on a wide display. Scattering bins into columns — the obvious loop
    // — strands every second column and stripes the picture.
    const columns = reduce([-40, -50, -60], 12);

    expect(Array.from(columns).every(Number.isFinite)).toBe(true);
  });

  it("treats a bin nobody measured as absent rather than as quiet", () => {
    // NaN is a dropped block. Painting it — or letting it win a max — would claim a
    // reading that was never taken.
    const columns = reduce([Number.NaN, Number.NaN], 1);

    expect(Number.isFinite(columns[0] as number)).toBe(false);
  });

  it("covers every bin, so a signal cannot fall between two columns", () => {
    const db = new Array(1000).fill(-90);
    db[999] = -10; // the very last bin, the one an off-by-one loses

    const columns = reduce(db, 37);

    expect(Math.max(...columns)).toBe(-10);
  });
});

describe("how tall one measurement row is drawn", () => {
  it("gives a slow stream whole pixels rather than one", () => {
    // MEASURED by the owner: at one row a second on the FM dial, one device pixel per
    // row left the picture nearly empty — seven minutes to fill a phone's height, where
    // the ring it replaced showed the same forty seconds two and a half times taller.
    expect(rowPixelsFor(180, 440)).toBe(2);
  });

  it("is always an integer, which is what keeps the draw 1:1", () => {
    // A fractional row height resamples on every scroll, which is the twinkle.
    for (const wanted of [7, 40, 180, 900, 1800]) {
      const px = rowPixelsFor(wanted, 440);
      expect(Number.isInteger(px)).toBe(true);
      expect(px).toBeGreaterThanOrEqual(1);
    }
  });

  it("never goes below one, however much history is wanted", () => {
    expect(rowPixelsFor(5000, 440)).toBe(1);
  });

  it("does not draw fat blocks for a very slow stream", () => {
    expect(rowPixelsFor(1, 440)).toBeLessThanOrEqual(4);
  });
});
