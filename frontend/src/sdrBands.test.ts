// The band table's client half — the three derivations the picker actually shows.
//
// None of these is arithmetic the PWA is allowed to invent: `sdrBands.ts` says at the
// top that every field arrives from the server, because a screen that worked the
// physics out itself is free to disagree with the radio that runs. What this file
// pins is the READING — which row the picker greys out, and what it says about a row
// it cannot make honest.
//
// It exists because the two most consequential of those readings had no test at all,
// and both are failures that look like success: a refusal that greys out ten bands the
// radio can in fact draw makes the feature INVISIBLE rather than broken, and an unsaid
// image caveat turns a station folded in from 21 MHz into a mystery signal on 40 m.

import { describe, expect, it } from "vitest";
import { type BandSection, dutyNote, imageNote, sectionAt, whyNotLive } from "./sdrBands";

function section(over: Partial<BandSection> = {}): BandSection {
  return {
    id: "2m-repeaters",
    band: "2 m",
    name: "Repeaters",
    start_hz: 146_000_000,
    stop_hz: 148_000_000,
    mode: "fm",
    step_hz: 5_000,
    channel_hz: 15_000,
    note: "The busiest listening segment.",
    live: "fast",
    continuous: false,
    sweep_seconds: 120,
    span_hz: 2_000_000,
    centre_hz: 147_000_000,
    hops: 1,
    duty: 1,
    surveyable: true,
    direct_sampling: false,
    sample_rate_hz: 2_400_000,
    fft_bins: 4_000,
    bin_hz: 600,
    image_start_hz: 0,
    image_stop_hz: 0,
    channels: [],
    ...over,
  };
}

/** A shortwave row as the server now sends it: unsurveyable, and drawable anyway. */
function shortwave(over: Partial<BandSection> = {}): BandSection {
  return section({
    id: "40m",
    band: "HF",
    name: "40 m",
    start_hz: 7_125_000,
    stop_hz: 7_300_000,
    span_hz: 175_000,
    centre_hz: 7_212_500,
    surveyable: false,
    direct_sampling: true,
    sample_rate_hz: 256_000,
    fft_bins: 1_024,
    bin_hz: 250,
    image_start_hz: 21_500_000,
    image_stop_hz: 21_675_000,
    ...over,
  });
}

describe("which rows the picker offers", () => {
  it("no longer greys out shortwave, which is the point of the whole change", () => {
    // It used to refuse on `surveyable`, which is rtl_power's answer — the tool
    // hardcodes direct sampling mode 1 and this board wires the Q branch. The live
    // picture is a capture and our own FFT, so it reaches down there and the flag
    // stops being the question.
    const forty = shortwave();

    expect(forty.surveyable).toBe(false);
    expect(whyNotLive(forty)).toBeNull();
  });

  it("still refuses a shortwave row with no capture behind it", () => {
    // Below 24 MHz a picture is one capture or nothing: the thing that stitches several
    // hops together is the tool that cannot go there at all.
    const refusal = whyNotLive(shortwave({ sample_rate_hz: 0, fft_bins: 0, bin_hz: 0 }));

    expect(refusal).toContain("one capture");
  });

  it("leaves an ordinary VHF row alone", () => {
    expect(whyNotLive(section())).toBeNull();
    // ...including the multi-hop tier, which rtl_power still serves perfectly well.
    expect(whyNotLive(section({ live: "slow", sample_rate_hz: 0, hops: 8 }))).toBeNull();
  });
});

describe("what a row says about itself", () => {
  it("names the band folded onto a shortwave row rather than raising a flag", () => {
    // 7.125-7.300 arrives summed with 21.500-21.675, reversed, because the ADC samples
    // at 28.8 MHz. Nothing in software separates the two contributions.
    expect(imageNote(shortwave())).toBe("Carries a reversed image of 21.50–21.68 MHz.");
  });

  it("says nothing at all above the tuner floor, where there is no fold", () => {
    expect(imageNote(section())).toBeNull();
  });

  it("still says what a multi-hop picture costs", () => {
    const note = dutyNote(section({ hops: 8, duty: 0.1 }));

    expect(note).toContain("8 hops");
    expect(note).toContain("10%");
  });

  it("says nothing about duty on a one-hop row, because there is nothing to say", () => {
    expect(dutyNote(section())).toBeNull();
  });
});

describe("matching a live range back to the section it came from", () => {
  it("matches on the edges, so a range that merely sits inside one is not it", () => {
    const rows = [section(), shortwave()];

    expect(sectionAt(rows, 7_125_000, 7_300_000)?.id).toBe("40m");
    expect(sectionAt(rows, 7_150_000, 7_200_000)).toBeNull();
  });
});
