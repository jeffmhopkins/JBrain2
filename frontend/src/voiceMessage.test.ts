import { describe, expect, it } from "vitest";

import { MAX_MESSAGE_MS, PANEL_RATE, normalisingGain, toPanelPcm } from "./voiceMessage";

/** An AudioBuffer stand-in: jsdom has no Web Audio, and the conversion under test only ever
 *  asks for `numberOfChannels`, `sampleRate`, `length` and `getChannelData`. */
function fakeBuffer(channels: Float32Array[], sampleRate: number): AudioBuffer {
  return {
    numberOfChannels: channels.length,
    sampleRate,
    length: channels[0]?.length ?? 0,
    duration: (channels[0]?.length ?? 0) / sampleRate,
    getChannelData: (i: number) => channels[i] as Float32Array,
  } as unknown as AudioBuffer;
}

describe("toPanelPcm", () => {
  it("resamples to the rate the panels speak", () => {
    // One second at 48 kHz in, one second at 16 kHz out.
    const input = new Float32Array(48_000);
    const out = new Int16Array(toPanelPcm(fakeBuffer([input], 48_000)));
    expect(out.length).toBe(PANEL_RATE);
  });

  it("averages channels rather than taking the first", () => {
    /* A laptop with a stereo array puts real signal in the second channel. Taking channel 0
       alone halves the level on that hardware, which reads as a quiet, failing microphone.
       Asserted as a RATIO between two samples rather than an absolute value, because
       normalisation now scales the whole clip and an absolute expectation here would only be
       re-deriving the target peak. */
    const left = new Float32Array([1, 1, 0.5, 0.5]);
    const right = new Float32Array([0, 0, 0.5, 0.5]);
    const out = new Int16Array(toPanelPcm(fakeBuffer([left, right], PANEL_RATE)));
    // Averaged: 0.5 then 0.5 — equal. Channel 0 alone would give 1.0 then 0.5.
    expect(out[0]).toBe(out[2]);
  });

  it("never produces a sample that wrapped through the Int16 cast", () => {
    /* The loudest possible click, and the same trap `cue.c` documents on the firmware side:
       (32767 * 1.5) through an Int16Array cast is not a loud sample, it is a sign flip.
       Normalisation should make this unreachable — a clip peaking at 1.5 is scaled DOWN — and
       the clamp stays as the guard for the paths that skip it. What matters is the property,
       not which of the two enforced it: a positive input must not come out negative. */
    const hot = new Float32Array([1.5, -1.5, 1.5, -1.5]);
    const out = new Int16Array(toPanelPcm(fakeBuffer([hot], PANEL_RATE)));
    expect(out[0]).toBeGreaterThan(0);
    expect(out[1]).toBeLessThan(0);
  });

  it("brings a quiet recording up to the level the box's own speech is rendered at", () => {
    /* The owner: *"my pwa recording seems to be way lower gain than the robot voices."* Kokoro
       renders near full scale and a microphone capture of someone talking normally peaks far
       below it, so his own voice arrived quieter than the same words read by the box — which
       is backwards. A tenth of full scale in must come out near it. */
    // -14 dBFS, which is where a phone at conversational distance lands.
    const quiet = new Float32Array(PANEL_RATE);
    for (let i = 0; i < quiet.length; i++) {
      quiet[i] = 0.2 * Math.sin((2 * Math.PI * 440 * i) / PANEL_RATE);
    }
    const out = new Int16Array(toPanelPcm(fakeBuffer([quiet], PANEL_RATE)));
    let peak = 0;
    for (const v of out) peak = Math.max(peak, Math.abs(v));
    expect(peak).toBeGreaterThan(28_000);
    expect(peak).toBeLessThanOrEqual(32_767);
  });

  it("lifts an even quieter one as far as the cap allows, and no further", () => {
    /* -20 dBFS gets the full 8x — 0.8 rather than the 0.89 target — and that shortfall is the
       cap doing its job rather than a miss. The alternative is a rule that also multiplies an
       empty room by eleven. */
    const faint = new Float32Array(PANEL_RATE);
    for (let i = 0; i < faint.length; i++) {
      faint[i] = 0.1 * Math.sin((2 * Math.PI * 440 * i) / PANEL_RATE);
    }
    const out = new Int16Array(toPanelPcm(fakeBuffer([faint], PANEL_RATE)));
    let peak = 0;
    for (const v of out) peak = Math.max(peak, Math.abs(v));
    // 8x of 0.1 full-scale, within a sample of rounding.
    expect(peak).toBeGreaterThan(25_000);
    expect(peak).toBeLessThan(28_000);
  });

  it("does not amplify an empty room", () => {
    /* Normalising is not the same as turning it up. Without a floor, a recording of silence is
       multiplied until the room hiss is at full scale — a pop-up on a child's wall playing
       amplified nothing. */
    const hiss = new Float32Array(1000);
    for (let i = 0; i < hiss.length; i++) hiss[i] = (i % 3 === 0 ? 1 : -1) * 0.0005;
    expect(normalisingGain(hiss)).toBe(1);
  });

  it("caps how far it will lift a quiet clip", () => {
    /* A phone held at arm's length deserves a rescue; hiss does not deserve 60 dB. */
    const faint = new Float32Array([0.01, -0.01, 0.01, -0.01]);
    expect(normalisingGain(faint)).toBeLessThanOrEqual(8);
    expect(normalisingGain(faint)).toBeGreaterThan(1);
  });

  it("turns a hot recording DOWN rather than clipping it", () => {
    const hot = new Float32Array([1.0, -1.0, 0.9, -0.9]);
    expect(normalisingGain(hot)).toBeLessThan(1);
  });

  it("produces two bytes per sample, which is what the box parses", () => {
    const input = new Float32Array(PANEL_RATE);
    const bytes = toPanelPcm(fakeBuffer([input], PANEL_RATE));
    expect(bytes.byteLength).toBe(PANEL_RATE * 2);
  });

  it("survives an empty recording without producing a malformed buffer", () => {
    const bytes = toPanelPcm(fakeBuffer([new Float32Array(0)], 48_000));
    expect(bytes.byteLength).toBe(0);
  });

  it("agrees with the box about how long a message may be", () => {
    /* `MAX_MESSAGE_MS` in backend/src/jbrain/api/jpanel.py. If the box's ceiling drops below
       this, a recording is cut on arrival with nothing said about it. */
    expect(MAX_MESSAGE_MS).toBe(20_000);
  });
});
