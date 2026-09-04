// How the radio's own numbers are printed. Both of these were wrong in a way that only
// showed on the box: a frequency a digit short of naming a channel, and a bin width
// rounded until it looked like a figure somebody had chosen.

import { describe, expect, it } from "vitest";
import { khz, mhz } from "./mhz";

describe("a frequency", () => {
  it("keeps the digit that names a channel", () => {
    // 5 kHz spacing on most of the narrowband plan, so two decimals is a digit short.
    // A rule that dropped to two above 100 MHz printed the APRS channel as 144.39.
    expect(mhz(144_390_000)).toBe("144.390");
    expect(mhz(146_940_000)).toBe("146.940");
  });

  it("prints the same way high and low", () => {
    expect(mhz(7_200_000)).toBe("7.200");
    expect(mhz(1_090_000_000)).toBe("1090.000");
  });
});

describe("a bandwidth", () => {
  it("keeps the decimal on a width the radio chose rather than one that was asked for", () => {
    // MEASURED on the box: 88-108 MHz asked for 25 kHz bins and rtl_power granted
    // 19531 Hz — the largest power-of-two division of its per-hop bandwidth no coarser
    // than the request. Rounded to "20" that reads as a round number somebody picked,
    // and the owner is left wondering where their 25 went.
    expect(khz(19_531)).toBe("19.5");
    expect(khz(25_000)).toBe("25.0");
  });

  it("drops it once the width is wide enough not to need it", () => {
    expect(khz(200_000)).toBe("200");
  });
});
