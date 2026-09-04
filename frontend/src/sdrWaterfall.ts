// How a waterfall turns dB into colour. The arithmetic only — no canvas, no React.
//
// Kept apart from the component because this is the half that can be wrong in a way
// nobody sees: a scale a few dB off does not throw, it just paints a band that looks
// empty. It is also the half that has to MATCH `sweep.waterfall_png`, so a still image
// of a sweep and the live picture of the same band are the same picture.

import type { SpectrumRow } from "./sdrSpectrum";

/** How many rows are taken before the colour scale is frozen.
 *
 *  **The scale is calibrated once, then held.** Re-taking it every row is the obvious
 *  thing and it is wrong: the picture then renormalises around whatever is on the air,
 *  so a strong carrier appearing makes the noise floor go dark and a band that has gone
 *  quiet blooms — exactly the two changes the owner is watching for, erased by the act
 *  of watching. Eight rows is enough for a stable percentile and is over in eight
 *  seconds. */
export const CALIBRATION_ROWS = 8;

/** Where the palette starts, as a percentile of the calibration rows. The same figures
 *  `sweep.waterfall_png` uses, because it is the same gesture: not a sensitivity
 *  control — the signal was always in the numbers — but the contrast slider, finding
 *  the floor and stretching the palette to begin just above it. */
const FLOOR_PERCENTILE = 0.2;
const CEIL_PERCENTILE = 0.995;
/** Never let the window collapse. A dead-flat band (a disconnected antenna, a radio
 *  measuring its own noise) has almost no spread, and a palette stretched across it
 *  turns rounding into a light show. */
const MIN_SPAN_DB = 12;

export interface Scale {
  lowDb: number;
  highDb: number;
}

function percentile(sorted: number[], fraction: number): number {
  if (sorted.length === 0) return 0;
  const at = Math.min(sorted.length - 1, Math.max(0, Math.round(fraction * (sorted.length - 1))));
  return sorted[at] as number;
}

/** The colour window for a band, from the rows seen so far. */
export function calibrate(rows: readonly SpectrumRow[]): Scale {
  const flat: number[] = [];
  for (const row of rows) {
    for (const db of row.db) if (Number.isFinite(db)) flat.push(db);
  }
  if (flat.length === 0) return { lowDb: -60, highDb: -20 };
  flat.sort((a, b) => a - b);
  const low = percentile(flat, FLOOR_PERCENTILE);
  const high = percentile(flat, CEIL_PERCENTILE);
  return { lowDb: low, highDb: Math.max(high, low + MIN_SPAN_DB) };
}

/** One bin's colour: dark blue → steel → amber, as `sweep.waterfall_png` paints it.
 *
 *  The app's own accents rather than a rainbow. A perceptually uneven palette invents
 *  edges the data does not have, and on a waterfall those read as signals. */
export function shade(db: number, scale: Scale): [number, number, number] {
  if (!Number.isFinite(db)) return [0, 0, 0];
  const span = Math.max(scale.highDb - scale.lowDb, 1e-6);
  const t = Math.min(1, Math.max(0, (db - scale.lowDb) / span));
  if (t < 0.5) {
    const u = t * 2;
    return [Math.round(14 + 40 * u), Math.round(15 + 90 * u), Math.round(24 + 130 * u)];
  }
  const u = (t - 0.5) * 2;
  return [Math.round(54 + 200 * u), Math.round(105 + 100 * u), Math.round(154 - 60 * u)];
}

/** Paint `rows` — newest first — into an RGBA buffer `bins` wide and `height` tall.
 *
 *  **Newest at the BOTTOM, scrolling up**, which is the owner's call (2026-09-04) and
 *  the convention a spectrum analyser uses: the live edge sits against the frequency
 *  axis it is measured on, and history rises away from it. This was built the other way
 *  round — receivers like SDR# and gqrx default to newest-at-top — and both are real
 *  conventions, so it is a preference rather than a correction.
 *
 *  Rows the history does not have yet are left transparent rather than black, so a
 *  picture that is still filling reads as empty rather than as a band with nothing on
 *  it. They are ABOVE the live edge now, which is also why the fill has to be indexed
 *  from the bottom rather than the picture simply being flipped: a half-full waterfall
 *  drawn upside down would put its blank half over the newest rows. */
export function paint(
  rows: readonly SpectrumRow[],
  bins: number,
  height: number,
  scale: Scale,
): Uint8ClampedArray<ArrayBuffer> {
  // An explicit ArrayBuffer, not the default: `ImageData` refuses a view that might sit
  // on a SharedArrayBuffer, and the inferred type is the union of both.
  const pixels = new Uint8ClampedArray(new ArrayBuffer(bins * height * 4));
  for (let age = 0; age < height && age < rows.length; age += 1) {
    // `age` counts back from the live edge; `y` puts it that far above the bottom.
    const y = height - 1 - age;
    const row = rows[age] as SpectrumRow;
    for (let x = 0; x < bins; x += 1) {
      // A row narrower than the canvas is a frame that lost a block. Left transparent,
      // because painting its floor colour would claim a measurement that was not taken.
      if (x >= row.db.length) continue;
      const [r, g, b] = shade(row.db[x] as number, scale);
      const at = (y * bins + x) * 4;
      pixels[at] = r;
      pixels[at + 1] = g;
      pixels[at + 2] = b;
      pixels[at + 3] = 255;
    }
  }
  return pixels;
}
