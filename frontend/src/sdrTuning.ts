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

/** ...and the same tolerance as a share of the PASSBAND, whichever is wider.
 *
 *  MEASURED ON AIR 2026-09-05, and this constant exists because the bin rule alone was
 *  wrong on the broadcast dial. An FM signal's INSTANTANEOUS spectrum is not symmetric
 *  — the carrier is swinging, which is what modulation is — so on a station 180 kHz
 *  wide the loudest part of any one 100 ms frame wanders tens of kHz either way.
 *  104.1, tuned exactly, read 39 kHz "high"; 96.5 read 2.3 kHz "low" on the same pass.
 *  Against one bin of tolerance both would have put a caret and a "Centre it" prompt
 *  on a station that is precisely where it should be.
 *
 *  5% of the passband is 800 Hz on a narrowband channel — still a real tuning error
 *  there — and 9 kHz on a broadcast one, which with the averaging below covers the
 *  wander without hiding a mistune worth acting on. */
export const CENTRED_SHARE = 0.05;

export interface Tuning {
  /** How far the signal sits from the tuned frequency. Positive is high. This is where
   *  it IS — what the marker on the picture points at. */
  offsetHz: number;
  /** How far it sits from where it SHOULD be — the middle of the demodulator's
   *  passband, which is not the dial on SSB (C14). This is the correction: what the
   *  readout says and what the "tune to it" button applies.
   *
   *  Identical to `offsetHz` on every symmetric mode, which is every mode but SSB. On
   *  `usb` a correctly tuned signal sits at +1850 Hz from the dial, and reporting THAT
   *  as the error told the owner to move a radio that was already right — and moving it
   *  would have put the signal at the dial, outside the +300..+3400 the demodulator
   *  hears. */
  errorHz: number;
  /** True when the ERROR is inside what the row can actually resolve. */
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

/** The noise this channel sits on, from the bins OUTSIDE the passband.
 *
 *  Not the row's median, which is what this used first and which is not a noise floor
 *  on a channel view: the row is cropped to twice the passband, so on a broadcast
 *  station 180 kHz wide in a 240 kHz row the signal is two thirds of the bins and the
 *  median lands inside it. The reading then came back NULL — "nothing in this channel"
 *  on a station reading 40 dB over the noise.
 *
 *  This mirrors `_channel_floor` in the sidecar's `server.py` deliberately: the probe
 *  and the picture answer "is it centred?" for the same rows, and a floor measured
 *  differently in the two places is a disagreement waiting for the marginal case. */
function floorOf(row: SpectrumRow): number {
  const { lowHz, highHz } = passbandEdges(row);
  const outside: number[] = [];
  for (let i = 0; i < row.db.length; i += 1) {
    // The bin's own centre frequency, as an offset from the row's middle — which is the
    // dial, and which the passband is NOT centred on for SSB. Taking a symmetric slice
    // off each end, as this did, put the top of a `usb` passband in the "noise".
    const hz = (i + 0.5 - row.db.length / 2) * row.binHz;
    if (hz < lowHz || hz > highHz) outside.push(row.db[i] as number);
  }
  if (outside.length === 0) return median(row.db);
  // A LOW QUARTILE, not the median (C21). The row reaches four times the passband, so on
  // the FM dial its outer thirds are where the raster puts the neighbour, and a
  // neighbour's skirt can fill a third of this ring — enough to drag a median off the
  // noise and onto the edge of another station. MEASURED ON AIR: beside a carrier at
  // 96.494 MHz, 96.3 read a ring floor of -46.9 dB where an empty 107.9 read -49.1.
  // The quartile sits in clean noise either way, and on an empty ring it is within a
  // decibel of the median it replaces.
  const sorted = [...outside].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 4)] as number;
}

/** Where the demodulator's passband sits, as offsets from the tuned frequency.
 *
 *  `passbandHz` is a WIDTH and `passbandCentreHz` says where its middle is — zero on
 *  every symmetric mode, +1850 on `usb`, -1850 on `lsb` (C14). Everything that asks
 *  "is this inside what we can hear?" goes through here so the two cannot drift. */
export function passbandEdges(row: SpectrumRow): { lowHz: number; highHz: number } {
  const half = row.passbandHz / 2;
  return { lowHz: row.passbandCentreHz - half, highHz: row.passbandCentreHz + half };
}

/** What is in this channel, or null when nothing is.
 *
 *  `tunedHz` is passed rather than derived from the row's midpoint because they are
 *  not always the same thing: the row is cropped to a whole number of bins, so its
 *  centre can sit half a bin off the frequency it was measured around. */
export function tuningOf(row: SpectrumRow, tunedHz: number): Tuning | null {
  if (row.passbandHz <= 0 || row.db.length === 0 || row.binHz <= 0) return null;
  // The peak is looked for INSIDE THE PASSBAND, not across the row (C21). The row
  // reaches four times the passband, so on the FM dial its outer thirds are exactly
  // where the 200 kHz raster puts the NEIGHBOUR — and a global argmax there answers
  // "where is the signal in this channel?" with a different station.
  //
  // MEASURED ON AIR beside a carrier at 96.494 MHz: at 96.3 the strongest bin in the row
  // sat at +157,969 Hz and at 96.7 at -161,250 Hz — both the same neighbour, 158 kHz
  // outside a 90 kHz passband. The readout would have called it 1.6 kHz off and the
  // "tune to it" button would have moved the dial onto it.
  const band = passbandEdges(row);
  let peak = Number.NEGATIVE_INFINITY;
  let peakAt = -1;
  for (let i = 0; i < row.db.length; i += 1) {
    const hz = (i + 0.5 - row.db.length / 2) * row.binHz;
    if (hz < band.lowHz || hz > band.highHz) continue;
    const value = row.db[i] as number;
    if (Number.isFinite(value) && value > peak) {
      peak = value;
      peakAt = i;
    }
  }
  if (peakAt < 0) return null;
  const floor = floorOf(row);
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

  // NO half bin (C20). `startHz` is bin 0's CENTRE — the sidecar's `iq.Spectrometer`
  // puts DC in bin `n / 2`, so `startHz + i * binHz` addresses bin `i` exactly. Adding
  // half a bin put this readout half a bin off the box's own peak frequencies: 46.9 Hz
  // on a 93.75 Hz channel row. Small, and exactly the kind of thing that is never
  // traced, because both numbers look right on their own.
  const hzOf = (bin: number) => row.startHz + bin * row.binHz - tunedHz;
  const fromHz = hzOf(low);
  const toHz = hzOf(high);
  const offsetHz = (fromHz + toHz) / 2;
  const { lowHz, highHz } = passbandEdges(row);
  const errorHz = offsetHz - row.passbandCentreHz;
  const width = toHz - fromHz + row.binHz;
  const inside = Math.max(0, Math.min(toHz, highHz) - Math.max(fromHz, lowHz) + row.binHz);
  return {
    offsetHz,
    errorHz,
    centred:
      Math.abs(errorHz) <= Math.max(CENTRED_BINS * row.binHz, CENTRED_SHARE * row.passbandHz),
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
  const khz = Math.abs(tuning.errorHz) / 1000;
  const shown = khz >= 10 ? khz.toFixed(0) : khz.toFixed(1);
  return `${shown} kHz ${tuning.errorHz > 0 ? "high" : "low"}`;
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
