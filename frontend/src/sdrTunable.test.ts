import { describe, expect, it } from "vitest";

import {
  MAX_MHZ,
  MIN_MHZ,
  NYQUIST_MHZ,
  TUNER_MIN_MHZ,
  whyNotSpannable,
  whyNotTunable,
} from "./sdrTunable";

describe("where the radio can be pointed", () => {
  it("reaches shortwave, which the tuner's own floor would refuse", () => {
    // The bug the owner hit: the band sheet bounded on the R820T2 TUNER's floor (24 MHz)
    // and refused a manual 4.625 MHz — while the picture behind it was drawing
    // 4.393-5.417 MHz from the 60 m button. The band table could go somewhere the box's
    // own entry field called unreachable.
    expect(whyNotTunable(4.625)).toBeNull();
    expect(whyNotTunable(5.0)).toBeNull();
    expect(whyNotTunable(7.2)).toBeNull();
    // ...and it still reaches everything above the tuner's floor.
    expect(whyNotTunable(162.55)).toBeNull();
    expect(whyNotTunable(99.3)).toBeNull();
  });

  it("refuses the second Nyquist zone by name", () => {
    // Ask for 18.1 and the radio hands back 10.7, mirrored, reporting a healthy session
    // at the frequency that was typed. Nothing in the audio says otherwise, so this is
    // refused rather than warned.
    const said = whyNotTunable(18.1);
    expect(said).toContain("10.700");
    expect(said).toContain(String(NYQUIST_MHZ));
    expect(said).toContain(String(TUNER_MIN_MHZ));
  });

  it("refuses past either end", () => {
    expect(whyNotTunable(MIN_MHZ - 0.01)).toContain("tunes");
    expect(whyNotTunable(MAX_MHZ + 1)).toContain("tunes");
    expect(whyNotTunable(Number.NaN)).toContain("tunes");
  });

  it("holds the edges themselves", () => {
    expect(whyNotTunable(MIN_MHZ)).toBeNull();
    expect(whyNotTunable(MAX_MHZ)).toBeNull();
    expect(whyNotTunable(NYQUIST_MHZ)).toBeNull();
    expect(whyNotTunable(TUNER_MIN_MHZ)).toBeNull();
  });
});

describe("where a picture can be drawn", () => {
  it("allows the owner's 4.625 MHz ± 0.5, which is the case that was refused", () => {
    expect(whyNotSpannable(4.125, 5.125)).toBeNull();
  });

  it("checks BOTH edges, not the centre", () => {
    // 20 MHz ± 8 has a centre inside the aliasing hole and edges outside it; 12 ± 4
    // has a legal centre and reaches into the hole. A centre-only test passes the
    // second and draws half a picture from a mirror of somewhere else.
    expect(whyNotSpannable(12, 28)).not.toBeNull();
    expect(whyNotSpannable(8, 16)).not.toBeNull();
    // ...while a span wholly inside one path is fine.
    expect(whyNotSpannable(4.393, 5.417)).toBeNull();
    expect(whyNotSpannable(88, 108)).toBeNull();
  });

  it("needs a width", () => {
    // The Go there button is disabled on a zero width anyway; this is what makes the
    // hint say why rather than going quiet.
    expect(whyNotSpannable(5, 5)).toBe("A picture needs a width.");
    expect(whyNotSpannable(5, 4)).toBe("A picture needs a width.");
    expect(whyNotSpannable(Number.NaN, 5)).toBe("A picture needs a width.");
  });
});
