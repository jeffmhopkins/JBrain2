// Counting in channels, against the plans that are not arithmetic.
//
// Every case here is a place where a step size gets a different answer than the list
// does — which is the whole reason the list exists. The failure mode is quiet: a dial
// that lands 10 kHz from a channel sounds exactly like a channel nobody is talking on.

import { describe, expect, it } from "vitest";
import type { BandSection } from "./sdrBands";
import {
  channelIndex,
  channelLabel,
  namedByFrequency,
  nearestChannel,
  planAt,
  stepChannel,
} from "./sdrChannels";

function plan(over: Partial<BandSection> = {}): BandSection {
  return {
    id: "cb",
    band: "CB",
    name: "Citizens band",
    start_hz: 26_965_000,
    stop_hz: 27_405_000,
    mode: "am",
    step_hz: 10_000,
    channel_hz: 10_000,
    note: "",
    live: "fast",
    continuous: false,
    sweep_seconds: 120,
    span_hz: 440_000,
    centre_hz: 27_185_000,
    hops: 1,
    duty: 1,
    surveyable: true,
    direct_sampling: false,
    sample_rate_hz: 1_024_000,
    fft_bins: 4_096,
    bin_hz: 250,
    image_start_hz: 0,
    image_stop_hz: 0,
    channel_plan: true,
    channels: [
      { hz: 27_205_000, name: "Ch 20", note: "" },
      { hz: 27_215_000, name: "Ch 21", note: "" },
      { hz: 27_225_000, name: "Ch 22", note: "" },
      // In CHANNEL order, which on CB is not frequency order.
      { hz: 27_255_000, name: "Ch 23", note: "" },
      { hz: 27_235_000, name: "Ch 24", note: "" },
      { hz: 27_245_000, name: "Ch 25", note: "" },
      { hz: 27_265_000, name: "Ch 26", note: "" },
    ],
    ...over,
  };
}

const CB = plan();
const AIR = plan({
  id: "air-tower",
  band: "Airband",
  start_hz: 118_000_000,
  stop_hz: 120_000_000,
  channel_plan: false,
  channels: [{ hz: 118_500_000, name: "Tower", note: "" }],
});

describe("finding a plan under the radio", () => {
  it("finds the one whose range contains the frequency", () => {
    expect(planAt([AIR, CB], 27_185_000)?.id).toBe("cb");
  });

  it("ignores a section whose channels are only landmarks", () => {
    // Airband is allocated per facility. Counting between the handful of named ones
    // would refuse most of the band, so the dial there stays a step size.
    expect(planAt([AIR], 118_500_000)).toBeNull();
  });

  it("is null off the end of every plan", () => {
    expect(planAt([CB], 145_000_000)).toBeNull();
  });

  it("includes the edges, which is where a band button parks the radio", () => {
    expect(planAt([CB], 26_965_000)?.id).toBe("cb");
    expect(planAt([CB], 27_405_000)?.id).toBe("cb");
  });
});

describe("which channel the radio is on", () => {
  it("finds it exactly", () => {
    expect(channelIndex(CB, 27_225_000)).toBe(2);
  });

  it("tolerates the hertz the PLL actually landed on", () => {
    // The radio reports what it achieved, not what it was asked: the R820T2's PLL
    // lands on a multiple of its own step. A tuner that called that "off channel"
    // would show `Off channel` on a channel it had just tuned to.
    expect(channelIndex(CB, 27_224_950)).toBe(2);
  });

  it("says nowhere when the frequency is genuinely between channels", () => {
    expect(channelIndex(CB, 27_230_000)).toBe(-1);
  });
});

describe("stepping", () => {
  it("counts up the PLAN, not the frequency", () => {
    // The case the whole module exists for: 22 → 23 is +30 kHz, over the top of 24 and
    // 25. Every CB radio does this, and a dial that stepped 10 kHz would not.
    expect(stepChannel(CB, 27_225_000, 1)?.name).toBe("Ch 23");
  });

  it("counts down the plan the same way", () => {
    expect(stepChannel(CB, 27_235_000, -1)?.name).toBe("Ch 23");
  });

  it("steps forwards to a LOWER frequency where the plan does", () => {
    const next = stepChannel(CB, 27_255_000, 1);

    expect(next?.name).toBe("Ch 24");
    expect(next?.hz).toBeLessThan(27_255_000);
  });

  it("stops at the top rather than wrapping", () => {
    // Wrapping would move the radio the whole width of the band on one tap, and the
    // owner leaning on + has no reason to expect it.
    expect(stepChannel(CB, 27_265_000, 1)).toBeNull();
  });

  it("stops at the bottom", () => {
    expect(stepChannel(CB, 27_205_000, -1)).toBeNull();
  });

  it("snaps to the nearest channel above when it starts between them", () => {
    // A typed frequency. Stepping by index from nowhere means nothing, and refusing
    // to move would strand it one tap from a channel it is sitting beside.
    expect(stepChannel(CB, 27_230_000, 1)?.name).toBe("Ch 24");
  });

  it("snaps downwards by FREQUENCY, not by channel number", () => {
    // From 27.230 the channel below is 22 at 27.225 — even though 24 and 25, whose
    // numbers are higher, are also nearby.
    expect(stepChannel(CB, 27_230_000, -1)?.name).toBe("Ch 22");
  });

  it("is null when there is nothing in that direction at all", () => {
    expect(stepChannel(CB, 27_400_000, 1)).toBeNull();
  });
});

describe("what the pill says", () => {
  it("names the channel", () => {
    expect(channelLabel(CB, 27_215_000)).toBe("Ch 21");
  });

  it("says so between channels rather than naming the nearest", () => {
    // Naming the nearest would claim the radio is somewhere it is not, on the one
    // control whose whole job is saying where it is.
    expect(channelLabel(CB, 27_230_000)).toBe("Off channel");
  });
});

describe("where a whole band opens", () => {
  it("lands on the channel nearest the centre, not the centre", () => {
    // The picker tunes to `centre_hz`, which is arithmetic on the section's edges. On
    // the FM dial that is 98.000, which 47 CFR 73.201 does not allow a station on — so
    // opening the dial would say `Off channel` on a band that is nothing but channels.
    expect(nearestChannel(CB, 27_231_000)?.name).toBe("Ch 24");
  });

  it("prefers the nearer of two either side", () => {
    expect(nearestChannel(CB, 27_218_000)?.name).toBe("Ch 21");
    expect(nearestChannel(CB, 27_222_000)?.name).toBe("Ch 22");
  });

  it("is null for a plan with no channels rather than throwing", () => {
    expect(nearestChannel(plan({ channels: [] }), 27_000_000)).toBeNull();
  });
});

describe("what a channel tile prints", () => {
  it("drops the frequency line where the name already is the frequency", () => {
    // The FM dial: a hundred tiles that would each say "101.5" over "101.500".
    expect(namedByFrequency({ hz: 101_500_000, name: "101.5", note: "" })).toBe(true);
  });

  it("keeps it for a numbered channel", () => {
    expect(namedByFrequency({ hz: 27_185_000, name: "Ch 19", note: "" })).toBe(false);
  });

  it("keeps it for the AM dial, which is named in kilohertz", () => {
    // "1010" is not 1010 MHz, and the tile has to say which.
    expect(namedByFrequency({ hz: 1_010_000, name: "1010", note: "" })).toBe(false);
  });
});
