// Live captions: opt-in, and honest about what they do not know.
//
// The two properties worth pinning are the ones a refactor could quietly lose: that
// turning captions off actually CLOSES the stream (leaving it open holds a whisper
// model resident on the box's GPU next to the chat model), and that a malformed or
// empty frame is skipped rather than rendered as a caption.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { playSdrAudio, resetSdrAudio } from "./sdrAudio";
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

beforeEach(() => vi.useFakeTimers());

afterEach(() => {
  resetSdrCaptions();
  resetSdrAudio();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Captions are HELD until their audio is heard, so a test must let the clock run. */
function tick(): void {
  vi.advanceTimersByTime(300);
}

/** Put the listener's ear at a known point on the box's clock. */
function earAt(anchor: number, played: number): void {
  playSdrAudio(anchor);
  const el = document.querySelector("audio");
  if (el) Object.defineProperty(el, "currentTime", { value: played, configurable: true });
}

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
    tick();

    const latest = sdrCaptions().latest;
    expect(latest?.text).toBe("winds south southeast");
    expect(latest?.words[1]).toEqual({ text: "southeast", confidence: 0.41 });
  });

  it("skips a frame with no text rather than showing an empty caption", () => {
    const stream = open();
    stream.send({ started_at: 1, text: "first", words: [] });
    tick();

    stream.send({ started_at: 2, text: "", words: [] });
    tick();

    // A squelched segment produces no text; the previous caption should stand rather
    // than being replaced by a blank plate over the waveform.
    expect(sdrCaptions().latest?.text).toBe("first");
  });

  it("survives a truncated frame", () => {
    const stream = open();
    stream.send({ started_at: 1, text: "first", words: [] });
    tick();

    stream.raw('{"started_at": 2, "text": "cut off');
    tick();

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

describe("caption timing", () => {
  it("holds a caption until its audio is actually heard", () => {
    // The whole sync fix. The box transcribes the LIVE EDGE, but playback runs a long
    // way behind it — measured at a steady ~8.3 s against the real sidecar. Showing a
    // caption on arrival puts the words on screen seconds BEFORE the speech, which
    // reads far worse than lagging.
    earAt(1000, 5); // the ear is at box-clock 1005
    const stream = open();

    stream.send({ started_at: 1010, text: "not heard yet", words: [] });
    tick();

    expect(sdrCaptions().latest).toBeNull();
  });

  it("shows it once playback reaches it", () => {
    earAt(1000, 5);
    const stream = open();
    stream.send({ started_at: 1001, text: "audible now", words: [] });

    tick();

    // startedAt 1001 + the small lead-in is at or before the ear's 1005.
    expect(sdrCaptions().latest?.text).toBe("audible now");
  });

  it("skips straight to the newest caption that has come due", () => {
    earAt(1000, 20); // the ear has moved on past several segments
    const stream = open();
    stream.send({ started_at: 1001, text: "old", words: [] });
    stream.send({ started_at: 1006, text: "newer", words: [] });
    stream.send({ started_at: 1011, text: "newest", words: [] });

    tick();

    // Flashing through captions whose audio is already past helps nobody.
    expect(sdrCaptions().latest?.text).toBe("newest");
  });

  it("shows captions anyway when the timeline cannot be anchored", () => {
    // No anchor (the session clock never arrived). Mistimed is bad; missing is worse.
    const stream = open();
    stream.send({ started_at: 5, text: "unanchored", words: [] });

    tick();

    expect(sdrCaptions().latest?.text).toBe("unanchored");
  });
});
