// The band table as the PWA sees it (`jbrain/sdr/bands.py`, `GET /api/sdr/bands`).
//
// **Nothing here is recomputed.** `hops`, `surveyable`, `bin_hz` and the image edges
// follow from the frequency and the hardware, and a screen that worked them out itself
// would be a second implementation of the physics — free to disagree with the radio that
// actually runs. Every field arrives from the server; this file only names them and says
// what the picker prints.
//
// Fetched once and held: the table is static, and the picker is how the owner learns
// what the box can do even when nothing is plugged in.

import { api } from "./api/client";

export interface BandChannel {
  hz: number;
  name: string;
  note: string;
}

export interface BandSection {
  id: string;
  band: string;
  name: string;
  start_hz: number;
  stop_hz: number;
  mode: string;
  step_hz: number;
  channel_hz: number;
  note: string;
  /** How this section can be watched live: `fast`, `slow`, or `none`. */
  live: string;
  continuous: boolean;
  sweep_seconds: number;
  span_hz: number;
  centre_hz: number;
  hops: number;
  /** Roughly the fraction of each interval any one bin is actually observed. Sent by
   *  the server, never derived here — see the note at the top of this file. */
  duty: number;
  /** False for every HF section: `rtl_power` hardcodes the ADC branch this hardware
   *  does not wire, so shortwave can be listened to and watched live, and never
   *  SURVEYED. It is no longer the same question as whether a waterfall can be drawn —
   *  see `whyNotLive`. */
  surveyable: boolean;
  /** True below 24 MHz, where the tuner is bypassed — and so where there is no gain
   *  control at all, because the tuner is powered down. */
  direct_sampling: boolean;
  /** What the radio digitises to draw this section in one hop, and 0 on the tiers
   *  rtl_power still serves. The picture is this WIDE, not as wide as the section. */
  sample_rate_hz: number;
  /** Bins in one live row — derived from the rate so `bin_hz` divides exactly, and
   *  deliberately not a fixed 4096. */
  fft_bins: number;
  /** `sample_rate_hz / fft_bins`, and 0 on the rtl_power tiers. Never rounded: the
   *  server refuses a pairing that does not divide, so a fraction arriving here would
   *  mean a frame mislabelled at its top edge. */
  bin_hz: number;
  /** The band that folds onto this one, reversed, and 0 where none does. The ADC
   *  samples at 28.8 MHz, so everything at `28.8 MHz − f` is SUMMED into the same bins;
   *  no software separates the two, which is why the honest thing is to name it. */
  image_start_hz: number;
  image_stop_hz: number;
  channels: BandChannel[];
}

export interface SdrBands {
  region: string;
  sections: BandSection[];
  tuner_min_hz: number;
  tuner_max_hz: number;
  direct_max_hz: number;
}

/** What to point a waterfall at: a curated section, or an explicit range.
 *
 *  Both, because both are real ways to ask. A section carries the mode, step and bin
 *  width someone chose while reading a band plan; the explicit range is the expert path
 *  and reaches anywhere the sweep tool does. The server takes exactly one of them. */
export interface SpectrumRange {
  section?: string;
  startMhz?: number;
  stopMhz?: number;
}

/** The sections grouped by band, in table order — which is browsing order: everyday
 *  bands first, amateur next, HF last because it needs a different antenna. */
export function byBand(sections: readonly BandSection[]): Array<[string, BandSection[]]> {
  const groups: Array<[string, BandSection[]]> = [];
  for (const section of sections) {
    const last = groups[groups.length - 1];
    if (last && last[0] === section.band) last[1].push(section);
    else groups.push([section.band, [section]]);
  }
  return groups;
}

/** The curated section a live range came from, or null for a hand-entered one.
 *
 *  Matched on the EDGES, not on containment: a manual range that happens to sit inside
 *  a section is not that section, and labelling it so would put a band name over a
 *  picture of something else. */
export function sectionAt(
  sections: readonly BandSection[],
  startHz: number,
  stopHz: number,
): BandSection | null {
  return sections.find((s) => s.start_hz === startHz && s.stop_hz === stopHz) ?? null;
}

/** Why this section cannot be drawn as a waterfall, or null.
 *
 *  A READING of what the server will answer, not a second rule: the route refuses the
 *  same thing in the same words. It exists so the picker can disable the row instead of
 *  offering a tap that ends in an error.
 *
 *  **It no longer asks `surveyable`.** That flag is `rtl_power`'s answer and it kept
 *  ten shortwave rows greyed out — the refusal that made HF invisible rather than
 *  merely absent. The live picture is a capture and our own FFT, which reaches
 *  shortwave through the ADC branch this board actually wires, so what is left to
 *  refuse down there is a section with no capture to draw it with. */
export function whyNotLive(section: BandSection): string | null {
  if (section.direct_sampling && !section.sample_rate_hz) {
    return "below 24 MHz a waterfall is one capture, and this range is wider than one";
  }
  return null;
}

/** The reversed image this section carries, in an operator's words, or null.
 *
 *  The honesty `mirrored` was supposed to buy and never did: it was false for every
 *  shortwave row while every one of them carries a fold of `28.8 MHz − f` summed into
 *  the same bins. Nothing in software can pull the two apart, so the only thing worth
 *  saying is which band is in there — otherwise a strong station at 21.4 MHz reads as
 *  a mystery signal on 40 m. */
export function imageNote(section: BandSection): string | null {
  if (!section.image_stop_hz) return null;
  const from = section.image_start_hz / 1_000_000;
  const to = section.image_stop_hz / 1_000_000;
  return `Carries a reversed image of ${from.toFixed(2)}–${to.toFixed(2)} MHz.`;
}

/** What a section's live tier costs the picture, in an operator's words.
 *
 *  The honesty this whole surface turns on: a span wide enough to need several retunes
 *  watches any given frequency for a fraction of each second, so a burst can fall
 *  between visits and leave no trace. A picture that hid that would look identical to
 *  one that could not miss anything. */
export function dutyNote(section: BandSection): string | null {
  if (section.hops <= 1) return null;
  const share = Math.max(1, Math.round(section.duty * 100));
  return `${section.hops} hops, so each frequency is watched about ${share}% of the time.`;
}

let cached: SdrBands | null = null;
let inFlight: Promise<SdrBands> | null = null;

/** The band table, fetched once per session. */
export async function loadBands(): Promise<SdrBands> {
  if (cached) return cached;
  if (!inFlight) {
    inFlight = api
      .getSdrBands()
      .then((bands) => {
        cached = bands;
        return bands;
      })
      .finally(() => {
        inFlight = null;
      });
  }
  return inFlight;
}

/** Test seam: forget the cached table between cases. */
export function resetBands(): void {
  cached = null;
  inFlight = null;
}
