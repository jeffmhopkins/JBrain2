import { describe, expect, it } from "vitest";

import { MAX_MESSAGE_MS, PANEL_RATE, toPanelPcm } from "./voiceMessage";

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
       alone halves the level on that hardware, which reads as a quiet, failing microphone. */
    const left = new Float32Array([1, 1, 1, 1]);
    const right = new Float32Array([0, 0, 0, 0]);
    const out = new Int16Array(toPanelPcm(fakeBuffer([left, right], PANEL_RATE)));
    expect(out[0]).toBe(Math.round(0.5 * 32767));
  });

  it("clamps before scaling, because a sample over 1.0 would wrap to full negative", () => {
    /* The loudest possible click, and the same trap `cue.c` documents on the firmware side:
       (32767 * 1.5) through an Int16Array cast is not a loud sample, it is a sign flip. */
    const hot = new Float32Array([1.5, -1.5]);
    const out = new Int16Array(toPanelPcm(fakeBuffer([hot], PANEL_RATE)));
    expect(out[0]).toBe(32767);
    expect(out[1]).toBe(-32767);
  });

  it("carries a signal through rather than flattening it", () => {
    const tone = new Float32Array(PANEL_RATE);
    for (let i = 0; i < tone.length; i++) tone[i] = Math.sin((2 * Math.PI * 440 * i) / PANEL_RATE);
    const out = new Int16Array(toPanelPcm(fakeBuffer([tone], PANEL_RATE)));
    let peak = 0;
    for (const s of out) peak = Math.max(peak, Math.abs(s));
    expect(peak).toBeGreaterThan(30_000);
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
