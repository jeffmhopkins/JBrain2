// Live captions: opt-in, and honest about what they do not know.
//
// The two properties worth pinning are the ones a refactor could quietly lose: that
// turning captions off actually CLOSES the stream (leaving it open holds a whisper
// model resident on the box's GPU next to the chat model), and that a malformed or
// empty frame is skipped rather than rendered as a caption.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  resetSdrCaptions,
  sdrCaptions,
  startSdrCaptions,
  stopSdrCaptions,
  subscribeSdrCaptions,
} from "./sdrCaptions";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  static CLOSED = 2;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  closed = false;
  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  send(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
  raw(text: string) {
    this.onmessage?.({ data: text } as MessageEvent<string>);
  }
}

function open(): FakeEventSource {
  vi.stubGlobal("EventSource", FakeEventSource);
  startSdrCaptions();
  return FakeEventSource.last as FakeEventSource;
}

afterEach(() => {
  resetSdrCaptions();
  vi.unstubAllGlobals();
});

describe("live captions", () => {
  it("are off until asked for", () => {
    expect(sdrCaptions().on).toBe(false);
    expect(sdrCaptions().latest).toBeNull();
  });

  it("carries per-word confidence through to the caption", () => {
    const stream = open();

    stream.send({
      started_at: 12,
      text: "winds south southeast",
      words: [
        { text: "winds", confidence: 0.94 },
        { text: "southeast", confidence: 0.41 },
      ],
    });

    // The tint is the point: narrowband voice degrades in a patterned way, and the
    // words it gets least sure of — numbers, place names — are usually the payload.
    const latest = sdrCaptions().latest;
    expect(latest?.text).toBe("winds south southeast");
    expect(latest?.words[1]).toEqual({ text: "southeast", confidence: 0.41 });
  });

  it("skips a frame with no text rather than showing an empty caption", () => {
    const stream = open();
    stream.send({ started_at: 1, text: "first", words: [] });

    stream.send({ started_at: 2, text: "", words: [] });

    // A squelched segment produces no text; the previous caption should stand rather
    // than being replaced by a blank plate over the waveform.
    expect(sdrCaptions().latest?.text).toBe("first");
  });

  it("survives a truncated frame", () => {
    const stream = open();
    stream.send({ started_at: 1, text: "first", words: [] });

    stream.raw('{"started_at": 2, "text": "cut off');

    expect(sdrCaptions().latest?.text).toBe("first");
  });

  it("reports a box that cannot caption at all", () => {
    const stream = open();
    stream.readyState = FakeEventSource.CLOSED;

    stream.onerror?.();

    // A 503 (no whisper gateway) closes the stream for good; a transient blip does
    // not, and EventSource reconnects on its own, so only the former is worth saying.
    expect(sdrCaptions().error).toContain("not available");
  });

  it("closes the stream when turned off, freeing the model", () => {
    const stream = open();

    stopSdrCaptions();

    // Left open, this holds whisper resident on the GPU beside the chat model for as
    // long as the session lasts — the exact cost the toggle exists to let the owner
    // decide about.
    expect(stream.closed).toBe(true);
    expect(sdrCaptions().on).toBe(false);
    expect(sdrCaptions().latest).toBeNull();
  });

  it("tells subscribers when a caption lands", () => {
    const seen = vi.fn();
    const stream = open();
    const off = subscribeSdrCaptions(seen);

    stream.send({ started_at: 3, text: "seas two to three feet", words: [] });

    expect(seen).toHaveBeenCalled();
    off();
  });
});
