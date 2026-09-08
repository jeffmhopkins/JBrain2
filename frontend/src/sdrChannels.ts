// Counting in channels, on the bands where channels are all there is.
//
// Binding spec: `docs/mocks/channel-tuning/a-channel-stepper.html` **shape A**, chosen
// 2026-09-07 — with the condition stated in the choice: *"it shouldn't ever be replacing
// the actual spectrum view"*. Nothing here draws anything. It answers one question for
// the tuner's ± and its pill — which channel is this, and what is the next one — and the
// picture above them is untouched.
//
// **Why a table rather than a step size.** On a channelised band the regulator has
// already listed every frequency a signal may legally occupy, and the list is not
// arithmetic: CB's 10 kHz raster has five gaps where the radio-control channels sit, and
// channel 23 sits at 27.255, ABOVE 24 and 25, because the 1977 expansion added three
// channels below the old top one instead of renumbering. Every CB radio ever built steps
// 22 → 23 → 24 backwards in frequency. A dial that "corrected" that would disagree with
// every other radio in earshot.

import type { BandChannel, BandSection } from "./sdrBands";

/** How far off a channel the radio may sit and still be called tuned to it.
 *
 *  Not zero: the radio reports what it actually achieved, and the R820T2's PLL lands on
 *  a multiple of its own step rather than exactly where it was asked. A hundred hertz is
 *  far under the narrowest spacing in any plan here (12.5 kHz) and far over the PLL's
 *  error, so it cannot merge two channels and cannot lose one. */
export const ON_CHANNEL_HZ = 100;

/**
 * The complete channel plan covering this frequency, or null.
 *
 * By CONTAINMENT, unlike `sectionAt` — the tuner knows a frequency, not a section, and
 * the question here is "is where I am somewhere with channels?". Sections without a
 * complete plan are invisible to this: their listed channels are landmarks, and counting
 * between landmarks would skip most of the band.
 */
export function planAt(sections: readonly BandSection[], hz: number): BandSection | null {
  return sections.find((s) => s.channel_plan && hz >= s.start_hz && hz <= s.stop_hz) ?? null;
}

/** Which channel of the plan the radio is on, as an index, or -1 between channels. */
export function channelIndex(plan: BandSection, hz: number): number {
  return plan.channels.findIndex((c) => Math.abs(c.hz - hz) <= ON_CHANNEL_HZ);
}

/**
 * Where ± lands, or null if there is nowhere to go.
 *
 * Two different moves behind one control, and the difference is the whole point:
 *
 * - **On a channel**, it steps by INDEX — the plan's own order, which is the radio's.
 *   From CB 22 that is channel 23 at 27.255, jumping over 24 and 25; from FRS 7 it is
 *   channel 15, back down 162 kHz, because 8-14 are the 467 MHz interstitials and are
 *   not in this band at all.
 * - **Between channels** — a typed frequency, or a band whose edge the radio was parked
 *   on — it snaps to the nearest channel in that direction, by FREQUENCY. Stepping by
 *   index from nowhere has no meaning, and refusing to move would strand a typed
 *   frequency one tap away from a channel it is sitting next to.
 */
export function stepChannel(plan: BandSection, hz: number, direction: number): BandChannel | null {
  const at = channelIndex(plan, hz);
  if (at >= 0) return plan.channels[at + direction] ?? null;
  const ahead = plan.channels
    .filter((c) => (direction > 0 ? c.hz > hz : c.hz < hz))
    .sort((a, b) => (direction > 0 ? a.hz - b.hz : b.hz - a.hz));
  return ahead[0] ?? null;
}

/** What the pill says: the channel's name, or the plan's name when between channels. */
export function channelLabel(plan: BandSection, hz: number): string {
  const on = plan.channels[channelIndex(plan, hz)];
  return on?.name ?? "Off channel";
}

/**
 * Where to land when a whole channelised band is chosen: the channel nearest its centre.
 *
 * The picker tunes to `centre_hz`, which is arithmetic on the section's edges and is
 * only a legal frequency by luck. On the FM dial it is 98.000, which cannot carry a
 * station — 47 CFR 73.201 allows odd tenths only — and on the AM dial it is 1.115 MHz,
 * between two channels. Landing there means the tuner says `Off channel` the instant a
 * band is opened, on a band whose whole point is that it has channels.
 */
export function nearestChannel(plan: BandSection, hz: number): BandChannel | null {
  return [...plan.channels].sort((a, b) => Math.abs(a.hz - hz) - Math.abs(b.hz - hz))[0] ?? null;
}

/**
 * Whether the channel's name already IS its frequency in MHz, so a tile need not print
 * both. True across the FM dial, where the name is "101.5" and the frequency 101.500 —
 * a hundred tiles each saying the same number twice.
 */
export function namedByFrequency(channel: BandChannel): boolean {
  return Number(channel.name) * 1_000_000 === channel.hz;
}
