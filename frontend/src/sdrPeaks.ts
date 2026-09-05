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

/** How far apart two peaks may be and still be the same signal, in bins — the floor
 *  used when the band has no channel raster. A carrier wanders a bin or two between
 *  rows as noise moves which bin of its own skirt is strongest, and a rule that
 *  required exactness would report that as a new station on every row. */
const SAME_SIGNAL_BINS = 3;

/** ...and as a share of the CHANNEL RASTER, which is what actually decides it.
 *
 *  REPORTED by the owner on the FM dial: 84 "signals" for a band with perhaps twenty
 *  stations on it, four pills for 96.5 alone at 96.438 / 96.475 / 96.503 / 96.541. A
 *  broadcast station is 180 kHz wide and its loudest bin wanders across that width from
 *  row to row, so three bins of 9.4 kHz — 28 kHz — matched almost nothing and every row
 *  added a station that was already there.
 *
 *  0.6 of the raster is 120 kHz on the dial, which covers the wander, and it cannot
 *  merge two real neighbours because the raster is what they are spaced by: 96.5 and
 *  96.7 stay two signals. On the 2 m plan it is 15 kHz of a 25 kHz channel. */
const SAME_SIGNAL_SHARE = 0.6;

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
  const tolerance = Math.max(
    Math.max(row.binHz, 1) * SAME_SIGNAL_BINS,
    row.channelHz * SAME_SIGNAL_SHARE,
  );
  const now = row.at;
  const kept: HeldPeak[] = [];
  const claimed = new Map<HeldPeak, SpectrumPeak>();

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
    if (match) claimed.set(match, found);
    else kept.push({ ...found, seen: now, live: true });
  }

  for (const candidate of held) {
    const found = claimed.get(candidate);
    if (found === undefined) {
      // Absent from this row, and KEPT anyway. Held has no timer: a band watched for an
      // hour should still be able to say what went through it, and an expiry means the
      // answer depends on when you happened to look. Clearing is the owner's, and a
      // retune clears it too — those markers belong to a band this picture no longer
      // covers.
      kept.push({ ...candidate, live: false });
      continue;
    }
    // A MATCHED signal keeps the frequency of its STRONGEST sighting, and this is what
    // makes a held pill tappable. It used to take the newest row's frequency instead,
    // so the number under the owner's thumb changed ten times a second and a
    // tap-then-confirm could not land twice on the same one. The strongest sighting is
    // also the best estimate of where the station actually is: the wander is noise
    // moving which bin of the skirt wins, and the skirt is loudest at the middle.
    kept.push(
      found.db > candidate.db
        ? { ...found, seen: now, live: true }
        : { ...candidate, db: candidate.db, seen: now, live: true },
    );
  }

  // Evicted by STRENGTH, then ordered by FREQUENCY. Two different questions: which to
  // forget when the list is full is about what is worth remembering, and what order to
  // read them in is about finding one — and a dial is read in frequency order. Sorting
  // the display by level meant every pill moved whenever a level wobbled, which is the
  // other half of why they could not be tapped twice.
  const survivors =
    kept.length > MAX_HELD ? [...kept].sort((a, b) => b.db - a.db).slice(0, MAX_HELD) : kept;
  return survivors.sort((a, b) => a.hz - b.hz);
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
