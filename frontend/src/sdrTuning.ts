// Reading a tuned channel off one spectrum row: where the signal is, and how far that
// is from where the radio is pointed.
//
// Separate from `sdrPeaks.ts` on purpose, because the question is a different one.
// `peaks.find` on the box answers "what stands above the noise across this BAND", and
// its baseline is a rolling median over 400 kHz — on a 32 kHz row that is the whole
// picture, and a signal filling 40% of it drags the median onto itself and vanishes.
// A tuning view asks something narrower and easier: there is one channel here, is
// anything in it, and is it centred.
//
// The answer is the MIDPOINT OF THE SHOULDERS, not the argmax. An FM carrier's top is
// flat and noisy, so the loudest bin hops around inside it several times a second and
// a readout built on that flickers between "+0.2 kHz" and "-0.3 kHz" while nothing is
// moving. The point halfway between the two 6 dB-down edges is the same number without
// the jitter, and for an asymmetric signal it is the more honest one anyway.

import type { SpectrumRow } from "./sdrSpectrum";

/** How far the loudest bin must stand over the row's own floor before this claims
 *  there is a signal at all. Below it the "centre" of a channel of noise is wherever
 *  the noise happened to peak, which is a reading with no content. */
export const SIGNAL_OVER_DB = 6;

/** Where the signal's edges are taken, below its peak. 6 dB is half power — the
 *  conventional width of anything, and far enough down the shoulder that a decibel of
 *  noise moves the edge by a fraction of a bin. */
export const EDGE_DOWN_DB = 6;

/** Inside this, the readout says "on centre" rather than a number.
 *
 *  Not zero, and not a fixed frequency: the finest thing the row can resolve is one
 *  bin, so a claim of "+40 Hz" from a row with 94 Hz bins is precision the measurement
 *  does not have. One bin either side is the honest floor. */
export const CENTRED_BINS = 1;

export interface Tuning {
  /** How far the signal sits from the tuned frequency. Positive is high. */
  offsetHz: number;
  /** True when the offset is inside what the row can actually resolve. */
  centred: boolean;
  /** The signal's 6 dB edges, as offsets from the tuned frequency. */
  fromHz: number;
  toHz: number;
  /** The loudest bin, in dBFS — a true RF figure, unlike `audio_peak`. */
  peakDb: number;
  /** How far that stands over the row's own floor. */
  overDb: number;
  /** How much of the signal falls outside the demodulator's passband, 0..1. What the
   *  strip is FOR: being 6 kHz off a 25 kHz channel is inaudible as a fault — the
   *  audio just sounds thin — while a third of the signal outside the shading is
   *  unmissable. */
  spilled: number;
}

/** The median of a copy. Sorting in place would reorder the row every frame, and the
 *  row is shared with whatever else is drawing it. */
function median(values: number[]): number {
  const clean = values.filter((v) => Number.isFinite(v));
  if (clean.length === 0) return Number.NaN;
  const sorted = [...clean].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2
    ? (sorted[mid] as number)
    : ((sorted[mid - 1] as number) + (sorted[mid] as number)) / 2;
}

/** What is in this channel, or null when nothing is.
 *
 *  `tunedHz` is passed rather than derived from the row's midpoint because they are
 *  not always the same thing: the row is cropped to a whole number of bins, so its
 *  centre can sit half a bin off the frequency it was measured around. */
export function tuningOf(row: SpectrumRow, tunedHz: number): Tuning | null {
  if (row.passbandHz <= 0 || row.db.length === 0 || row.binHz <= 0) return null;
  let peak = Number.NEGATIVE_INFINITY;
  let peakAt = -1;
  for (let i = 0; i < row.db.length; i += 1) {
    const value = row.db[i] as number;
    if (Number.isFinite(value) && value > peak) {
      peak = value;
      peakAt = i;
    }
  }
  if (peakAt < 0) return null;
  const floor = median(row.db);
  const over = Number.isFinite(floor) ? peak - floor : 0;
  if (!(over >= SIGNAL_OVER_DB)) return null;

  // Walk out from the peak rather than scanning the whole row for anything above the
  // threshold: a second station inside the view would otherwise be swept into the
  // same "signal" and drag its midpoint between the two.
  const edge = peak - EDGE_DOWN_DB;
  let low = peakAt;
  while (low > 0 && (row.db[low - 1] as number) >= edge) low -= 1;
  let high = peakAt;
  while (high < row.db.length - 1 && (row.db[high + 1] as number) >= edge) high += 1;

  const hzOf = (bin: number) => row.startHz + (bin + 0.5) * row.binHz - tunedHz;
  const fromHz = hzOf(low);
  const toHz = hzOf(high);
  const offsetHz = (fromHz + toHz) / 2;
  const half = row.passbandHz / 2;
  const width = toHz - fromHz + row.binHz;
  const inside = Math.max(0, Math.min(toHz, half) - Math.max(fromHz, -half) + row.binHz);
  return {
    offsetHz,
    centred: Math.abs(offsetHz) <= CENTRED_BINS * row.binHz,
    fromHz,
    toHz,
    peakDb: peak,
    overDb: over,
    spilled: width > 0 ? Math.max(0, Math.min(1, 1 - inside / width)) : 0,
  };
}

/** The offset as the readout says it. Never more precision than a row can carry. */
export function offsetLabel(tuning: Tuning): string {
  if (tuning.centred) return "On centre";
  const khz = Math.abs(tuning.offsetHz) / 1000;
  const shown = khz >= 10 ? khz.toFixed(0) : khz.toFixed(1);
  return `${shown} kHz ${tuning.offsetHz > 0 ? "high" : "low"}`;
}

/** The half of the sentence that says why it matters, or "" when it does not.
 *
 *  Only spoken when a real part of the signal is outside the passband: an offset
 *  smaller than that is a number the owner can see on the picture and does not need
 *  a sentence about. */
export function spillLabel(tuning: Tuning): string {
  if (tuning.spilled < 0.12) return "";
  const share = tuning.spilled >= 0.45 ? "half" : tuning.spilled >= 0.28 ? "a third" : "part";
  return ` — ${share} of it is outside the passband`;
}
