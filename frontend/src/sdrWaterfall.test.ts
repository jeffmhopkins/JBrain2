// The half of a waterfall that can be wrong without anyone noticing.
//
// A colour scale a few dB out does not throw — it paints a busy band that looks empty,
// or a quiet one that looks alive. So the scale, the ramp and the bin-to-pixel mapping
// are checked here as arithmetic, where a wrong answer is a failure rather than a
// picture nobody questions.

import { describe, expect, it } from "vitest";
import type { SpectrumRow } from "./sdrSpectrum";
import { CALIBRATION_ROWS, calibrate, paint, shade } from "./sdrWaterfall";

function row(db: number[], startHz = 144_000_000, binHz = 25_000): SpectrumRow {
  return { at: 0, startHz, stopHz: startHz + db.length * binHz, binHz, db };
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
    expect(CALIBRATION_ROWS).toBeGreaterThan(1);
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
    const pixels = paint([row([-40, -40])], 4, 1, scale);

    expect(pixels[3]).toBe(255);
    expect(pixels[7]).toBe(255);
    expect(pixels[11]).toBe(0);
    expect(pixels[15]).toBe(0);
  });

  it("fills exactly the buffer an ImageData of that size wants", () => {
    expect(paint([row([-40, -50, -60])], 3, 4, scale).length).toBe(3 * 4 * 4);
  });
});
