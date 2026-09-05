/** The signals a band is showing, and how long one stays on screen after it stops.
 *
 *  The rows arrive with their peaks already found (`peaks.py` on the box, so the agent
 *  and the picture cannot disagree about what was on the air). What is left here is the
 *  question a single row cannot answer: **is this the same signal I saw a moment ago?**
 *  A repeater that keys up for four seconds is one marker that appears and goes, not
 *  forty markers; and a marginal carrier that clears the threshold on alternate rows is
 *  one signal flickering, which is the reading that makes a live list unwatchable.
 *
 *  So the viewer holds them, and the toggle chooses which reading is on screen:
 *
 *    live   what the newest row found, and nothing else
 *    held   everything seen inside `HOLD_SECONDS`, whether or not it is there now
 */

import type { SpectrumPeak, SpectrumRow } from "./sdrSpectrum";

/** The most signals held at once. Held has no timer — it keeps what it has seen until
 *  the owner clears it — so this is the only bound, and it exists because a wideband
 *  scan left all night would otherwise accumulate without limit. Generous, because on
 *  any real band the count is the number of DISTINCT frequencies rather than of rows,
 *  and the weakest go first: what a held list is for is remembering what was there. */
export const MAX_HELD = 200;

/** How far apart two peaks may be and still be the same signal, in bins. A carrier
 *  wanders a bin or two between rows as noise moves which bin of its own skirt is
 *  strongest, and a rule that required exactness would report that as a new station on
 *  every row — the flicker this file exists to remove, reintroduced by its own matcher. */
const SAME_SIGNAL_BINS = 3;

export interface HeldPeak extends SpectrumPeak {
  /** Box clock of the newest row this was seen in. */
  seen: number;
  /** Whether the row that just arrived found it, as opposed to it being remembered. */
  live: boolean;
}

/** Fold one row's findings into what is already on screen.
 *
 *  Pure, and takes the clock off the ROW rather than reading it: rows carry the box's
 *  clock, and holding them against the browser's would drift against the very timestamps
 *  being compared. */
export function mergePeaks(held: readonly HeldPeak[], row: SpectrumRow): HeldPeak[] {
  const tolerance = Math.max(row.binHz, 1) * SAME_SIGNAL_BINS;
  const now = row.at;
  const kept: HeldPeak[] = [];
  const claimed = new Set<HeldPeak>();

  for (const found of row.peaks) {
    // The CLOSEST held signal within tolerance, not the first: two real stations a few
    // bins apart would otherwise take turns claiming each other's marker.
    let match: HeldPeak | null = null;
    let best = Number.POSITIVE_INFINITY;
    for (const candidate of held) {
      if (claimed.has(candidate)) continue;
      const gap = Math.abs(candidate.hz - found.hz);
      if (gap <= tolerance && gap < best) {
        best = gap;
        match = candidate;
      }
    }
    if (match) claimed.add(match);
    kept.push({ ...found, seen: now, live: true });
  }

  for (const candidate of held) {
    // Absent from this row, and KEPT anyway. Held has no timer: a band watched for an
    // hour should still be able to say what went through it, and an expiry means the
    // answer depends on when you happened to look. Clearing is the owner's, and a
    // retune clears it too — those markers belong to a band this picture no longer
    // covers.
    if (!claimed.has(candidate)) kept.push({ ...candidate, live: false });
  }

  kept.sort((a, b) => b.db - a.db);
  // The weakest go first if it ever comes to that: the point of holding is remembering
  // what was there, and the loudest is the likeliest to be wanted.
  return kept.length > MAX_HELD ? kept.slice(0, MAX_HELD) : kept;
}

/** Where a signal sits across the picture, 0 to 1, or null when it is off the edge.
 *
 *  Null rather than a clamp: a marker pinned to the edge claims a signal at a frequency
 *  the row does not cover, and the band changes under this view on every retune. */
export function positionOf(hz: number, row: SpectrumRow): number | null {
  const span = row.stopHz - row.startHz;
  if (!(span > 0)) return null;
  const at = (hz - row.startHz) / span;
  return at >= 0 && at <= 1 ? at : null;
}

/** What the chosen mode shows. `off` is handled by not rendering at all. */
export function visiblePeaks(held: readonly HeldPeak[], mode: "live" | "held"): HeldPeak[] {
  return mode === "live" ? held.filter((peak) => peak.live) : [...held];
}

/** How much of the picture's width one label needs to itself. A frequency is six
 *  characters at micro size, which on a phone's 360 CSS pixels is about a twelfth of the
 *  span — so anything closer than this overlaps the one beside it. */
const LABEL_SHARE = 0.085;

/** Which markers get to carry their frequency.
 *
 *  MEASURED by the owner, on the FM dial in a city: fourteen stations across 20 MHz put
 *  labels on top of each other, and the middle of the band read as
 *  `99 30001309106 923 66204.105…` — text that is worse than no text, because it looks
 *  like a measurement.
 *
 *  Every marker keeps its LINE. Only the label is rationed, strongest first, so what a
 *  glance can read is the loudest thing in each part of the band and the rest are still
 *  drawn where they are. The list underneath carries all of them, which is what a label
 *  is short for anyway. */
export function labelled(
  placed: readonly { peak: { hz: number; db: number }; at: number }[],
): Set<number> {
  const taken: number[] = [];
  const out = new Set<number>();
  // Strongest first so a crowded stretch keeps the signal worth naming, rather than
  // whichever of them happens to sit furthest left.
  for (const { peak, at } of [...placed].sort((a, b) => b.peak.db - a.peak.db)) {
    if (taken.some((other) => Math.abs(other - at) < LABEL_SHARE)) continue;
    taken.push(at);
    out.add(peak.hz);
  }
  return out;
}
