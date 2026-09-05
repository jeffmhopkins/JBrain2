// How a waterfall turns dB into colour, and how many rows a picture of it is worth.
// The arithmetic only — no canvas, no React.
//
// Kept apart from the component because this is the half that can be wrong in a way
// nobody sees: a scale a few dB off does not throw, it just paints a band that looks
// empty. It is also the half that has to MATCH `sweep.waterfall_png`, so a still image
// of a sweep and the live picture of the same band are the same picture.
//
// **Everything here is sized in SECONDS, not in rows.** A row was a second when the
// only engine was `rtl_power`, whose interval is an int clamped at 1 in the C. The I/Q
// engine runs the same stream at ~10 fps, so a constant counted in rows silently means
// a tenth of what it says: "three minutes of history" becomes eighteen seconds and
// "calibrated over eight seconds" becomes eight tenths of one. Both are measured off
// the box's own clock, which every row carries.

import type { SpectrumRow } from "./sdrSpectrum";

/** How much history the picture holds, and how long the colour scale is taken over.
 *  Three minutes is long enough that a net or a repeater conversation is visible as a
 *  shape; eight seconds is long enough for a stable percentile and short enough that
 *  the first sight of a band is not painted flat. */
export const HISTORY_SECONDS = 180;
export const CALIBRATION_SECONDS = 8;

/** The scale is not frozen off fewer rows than this however long they took, because a
 *  percentile of two rows is a percentile of whatever happened twice. */
const CALIBRATION_MIN_ROWS = 8;

/** The rate assumed until the stream has shown one: the `rtl_power` tier, which is what
 *  this picture has always been sized for. Guessing fast and being wrong would allocate
 *  a canvas ten times too tall on a stream that never fills it. */
const ASSUMED_FPS = 1;
/** The fastest rate the picture will size itself for. §2 of the I/Q plan puts the fast
 *  tier at ~10 fps; anything above that is a clock that glitched, and a canvas sized off
 *  it is memory spent on a stream the box cannot produce. */
const MAX_FPS = 10;
/** Gaps needed before a rate is believed. Four is enough for a median to survive one
 *  stalled frame, and at 1 fps it is over in five seconds. */
const RATE_GAPS = 4;
/** A floor, so a pathologically wide row cannot leave a picture with no height at all. */
const MIN_ROWS = 8;
/** The canvas the ring buffer lives on is `bins x rows`, and a browser refuses one past
 *  a few million pixels. At the sidecar's own 4096-bin ceiling and 10 fps the picture
 *  wants 7.4 M, so this does not bind today — it is here so that finer bins (plan F7)
 *  cost history rather than costing the picture entirely. */
const MAX_PIXELS = 8_000_000;

export interface Scale {
  lowDb: number;
  highDb: number;
}

/** The rate the stream is actually running at, in rows a second, or null while too few
 *  rows have arrived to say.
 *
 *  Measured off `Frame.at` — the box's clock, stamped when the row was measured — rather
 *  than off arrival times, which a WAN bunches up and a reconnect reorders. The MEDIAN
 *  gap, not the mean: one stalled frame or one retune barrier leaves a single huge gap,
 *  and an average over it would halve the history the picture keeps. Rows are newest
 *  first, the order the component holds them in. */
export function frameRate(rows: readonly SpectrumRow[]): number | null {
  const gaps: number[] = [];
  for (let i = 1; i < rows.length && gaps.length < RATE_GAPS; i += 1) {
    const gap = (rows[i - 1] as SpectrumRow).at - (rows[i] as SpectrumRow).at;
    // A row with no clock on it stamps `at` 0, so a gap of zero or less is not a fast
    // stream — it is a stream that cannot be timed, and it must not read as one.
    if (Number.isFinite(gap) && gap > 0) gaps.push(gap);
  }
  if (gaps.length < RATE_GAPS) return null;
  gaps.sort((a, b) => a - b);
  const middle = gaps[Math.floor(gaps.length / 2)] as number;
  return 1 / middle;
}

/** How many rows are three minutes at this rate, for a picture this wide. */
export function historyRows(fps: number | null, bins: number): number {
  const wanted = Math.round(HISTORY_SECONDS * Math.min(fps ?? ASSUMED_FPS, MAX_FPS));
  const budget = bins > 0 ? Math.floor(MAX_PIXELS / bins) : wanted;
  return Math.max(MIN_ROWS, Math.min(wanted, budget));
}

/** Whether the colour window has been given long enough to be held.
 *
 *  **The scale is calibrated once, then held.** Re-taking it every row is the obvious
 *  thing and it is wrong: the picture then renormalises around whatever is on the air,
 *  so a strong carrier appearing makes the noise floor go dark and a band that has gone
 *  quiet blooms — exactly the two changes the owner is watching for, erased by the act
 *  of watching.
 *
 *  Freezing it too EARLY is the opposite failure and the worse one, because it is
 *  silent: a radio that has just retuned is still settling, and a window taken off eight
 *  tenths of a second of that paints the whole rest of the session wrong. So the test is
 *  the span of box clock the rows cover, not how many of them there are. A stream whose
 *  rows carry no usable clock falls back to the row count, which is what this file did
 *  when a row could only ever be a second. */
export function calibrated(rows: readonly SpectrumRow[]): boolean {
  if (rows.length < CALIBRATION_MIN_ROWS) return false;
  const span = (rows[0] as SpectrumRow).at - (rows[rows.length - 1] as SpectrumRow).at;
  if (!Number.isFinite(span) || span <= 0) return true;
  return span >= CALIBRATION_SECONDS;
}

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
export function reduce(db: readonly number[], columns: number, extent = 0): Float64Array {
  /* Bins to pixel columns, by MAX-HOLD, computed here rather than by the browser.
   *
   *  A 4096-bin frame across 800 device pixels is five bins a pixel, and the question
   *  is which of the five the pixel shows. Left to `drawImage`, the answer is a
   *  bilinear blend of two of them chosen by the filter's phase — so the pixel's value
   *  depends on a resampling decision rather than on the measurement, and it CHANGES
   *  when anything about the draw changes. That is the twinkle: noise resampled
   *  differently on each paint.
   *
   *  Max rather than mean, which is the spectrum-display convention and not a
   *  preference: a carrier occupying one bin of five is the thing the owner is looking
   *  for, and a mean buries it four-fifths of the way into the noise. Max-hold makes a
   *  narrow signal survive being made narrower than a pixel.
   *
   *  Deterministic by construction: the same bins always produce the same column, so a
   *  column can only change when its numbers do.
   *
   *  `extent` is the band's FULL width in bins, which is not always this row's own: a
   *  frame that lost a block arrives short, and its tail must stay transparent rather
   *  than be stretched across the picture, which would claim a measurement never taken.
   *  Zero means "as wide as this row", for a caller with only one row to go on. */
  const out = new Float64Array(columns).fill(Number.NEGATIVE_INFINITY);
  const width = extent > 0 ? extent : db.length;
  if (width === 0 || columns <= 0) return out;
  for (let x = 0; x < columns; x += 1) {
    // GATHERED per column rather than scattered per bin. Scattering is the obvious loop
    // and it fails in the other direction: a 1024-bin band on a 1600-pixel display
    // leaves every second column untouched, so the picture comes out striped.
    const lo = Math.floor((x * width) / columns);
    // At least one bin per column, so no column is empty for want of one.
    const hi = Math.max(lo + 1, Math.floor(((x + 1) * width) / columns));
    let best = Number.NEGATIVE_INFINITY;
    for (let bin = lo; bin < hi && bin < db.length; bin += 1) {
      const value = db[bin] as number;
      // NaN is a bin nobody measured — a dropped block, not a quiet one — and must
      // neither win a max nor be painted. `>` is already false for NaN, which is why
      // this reads as though it ignored the case it handles.
      if (value > best) best = value;
    }
    out[x] = best;
  }
  return out;
}

export function paint(
  rows: readonly SpectrumRow[],
  columns: number,
  height: number,
  scale: Scale,
  extent = 0,
): Uint8ClampedArray<ArrayBuffer> {
  // An explicit ArrayBuffer, not the default: `ImageData` refuses a view that might sit
  // on a SharedArrayBuffer, and the inferred type is the union of both.
  const pixels = new Uint8ClampedArray(new ArrayBuffer(columns * height * 4));
  for (let age = 0; age < height && age < rows.length; age += 1) {
    // `age` counts back from the live edge; `y` puts it that far above the bottom.
    const y = height - 1 - age;
    const row = rows[age] as SpectrumRow;
    const reduced = reduce(row.db, columns, extent);
    for (let x = 0; x < columns; x += 1) {
      // A column no bin reached is a frame that lost a block. Left transparent, because
      // painting its floor colour would claim a measurement that was not taken.
      const value = reduced[x] as number;
      if (!Number.isFinite(value)) continue;
      const [r, g, b] = shade(value, scale);
      const at = (y * columns + x) * 4;
      pixels[at] = r;
      pixels[at + 1] = g;
      pixels[at + 2] = b;
      pixels[at + 3] = 255;
    }
  }
  return pixels;
}
