// The waterfall's canvas half: what it does per ROW, and what it does per FRAME.
//
// The colour arithmetic is checked in sdrWaterfall.test.ts, where a wrong answer is a
// number rather than a picture. What is left here is the half that only goes wrong at
// speed: a stream that runs ten times faster than the one this was written for must not
// cost ten times the paint, must not shrink three minutes of history to eighteen
// seconds, must not freeze its colour window off eight tenths of a second, and must not
// mistake a hertz of readback jitter for a retune. Every one of those is silent — the
// picture still draws, it is just wrong or the phone is just hot.

import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetSdrSpectrum, startSdrSpectrum } from "../sdrSpectrum";
import { SdrWaterfall } from "./SdrWaterfall";

/** The stream the store opens, held so a test can deliver a row through it — the same
 *  path a real one takes, rather than a second way of getting a row in. */
class FakeSource {
  static last: FakeSource | null = null;
  static readonly CLOSED = 2;
  readyState = 1;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor() {
    FakeSource.last = this;
  }

  close(): void {}
}

/** jsdom has no canvas at all, so both canvases are recorded instead of drawn. What is
 *  asserted on is the SHAPE of the work: one `putImageData` per row (a whole picture or
 *  a single row), and one clear-and-draw per visible paint. */
class Recorder {
  imageSmoothingEnabled = false;
  paints = 0;
  writes: { height: number; y: number }[] = [];

  constructor(readonly canvas: HTMLCanvasElement) {}

  clearRect(): void {
    this.paints += 1;
  }

  drawImage(): void {}

  putImageData(image: { height: number }, _x: number, y: number): void {
    this.writes.push({ height: image.height, y });
  }
}

class FakeImageData {
  constructor(
    readonly data: Uint8ClampedArray,
    readonly width: number,
    readonly height: number,
  ) {}
}

let contexts: Recorder[] = [];
let frames: (() => void)[] = [];

/** Run whatever the component asked to draw on the next animation frame. */
function flushFrame(): void {
  const due = frames;
  frames = [];
  act(() => {
    for (const run of due) run();
  });
}

/** The line under the picture, whose text React splits across several nodes. */
function note(): string {
  return document.querySelector(".wf-note")?.textContent ?? "";
}

/** The canvas the owner sees, and the ring buffer behind it. */
function onscreen(): Recorder {
  const found = contexts.find((c) => c.canvas.classList.contains("wf-canvas"));
  if (!found) throw new Error("the visible canvas was never drawn on");
  return found;
}

function ring(): Recorder {
  const found = contexts.find((c) => !c.canvas.classList.contains("wf-canvas"));
  if (!found) throw new Error("no offscreen canvas was made");
  return found;
}

beforeEach(() => {
  contexts = [];
  frames = [];
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (
    this: HTMLCanvasElement,
  ) {
    const known = contexts.find((c) => c.canvas === this);
    if (known) return known as unknown as CanvasRenderingContext2D;
    const made = new Recorder(this);
    contexts.push(made);
    return made as unknown as CanvasRenderingContext2D;
  } as unknown as typeof HTMLCanvasElement.prototype.getContext);
  vi.stubGlobal("ImageData", FakeImageData);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    frames.push(() => cb(0));
    return frames.length; // never 0 — the component uses the handle as its "asked" flag
  });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
  vi.stubGlobal("EventSource", FakeSource);
});

afterEach(() => {
  resetSdrSpectrum();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A watching picture, and the stream feeding it. */
function watching(): FakeSource {
  render(<SdrWaterfall />);
  act(() => startSdrSpectrum());
  const stream = FakeSource.last;
  if (!stream) throw new Error("no stream opened");
  return stream;
}

/** One row on the box clock, at 144.000 MHz in 25 kHz bins unless a test says otherwise. */
function send(stream: FakeSource, at: number, over: Record<string, unknown> = {}): void {
  const payload = JSON.stringify({
    at,
    start_hz: 144_000_000,
    bin_hz: 25_000,
    db: [-70, -60, -55, -50],
    ...over,
  });
  act(() => stream.onmessage?.(new MessageEvent("message", { data: payload })));
}

/** `count` rows arriving `gap` seconds apart, as the box would stamp them. */
function run(stream: FakeSource, count: number, gap: number, from = 1000): number {
  let at = from;
  for (let i = 0; i < count; i += 1) {
    send(stream, at);
    at += gap;
  }
  return at;
}

describe("what a row costs", () => {
  it("paints once a frame however many rows land in it", () => {
    // The bug this replaces: `blit()` inside the per-row subscription, repainting the
    // whole history every time. At 4096 bins and 10 fps that is 7.4 M shade() calls a
    // second on a phone's main thread — for frames a display cannot show anyway.
    const stream = watching();

    run(stream, 12, 0.1);

    expect(onscreen().paints).toBe(0);
    flushFrame();
    expect(onscreen().paints).toBe(1);
  });

  it("drops the paint and never the row", () => {
    // Coalescing is only safe because the row is already in the ring by then. A dropped
    // ROW would be a hole in the time base that no later frame can fill in.
    const stream = watching();

    run(stream, 12, 0.1);

    expect(ring().writes).toHaveLength(12);
  });

  it("writes one row into the ring once the window is held", () => {
    // The whole point: a new row costs a row of work, not a picture of work.
    const stream = watching();
    run(stream, 12, 1);

    const last = ring().writes.at(-1);

    expect(last?.height).toBe(1);
  });

  it("still repaints the picture when the canvas is resized", () => {
    // The pixels live in the ring rather than in an ImageData rebuilt per row, so a
    // resize is a re-blit and costs no colour arithmetic at all.
    const stream = watching();
    run(stream, 12, 1);
    flushFrame();

    act(() => window.dispatchEvent(new Event("resize")));
    flushFrame();

    expect(onscreen().paints).toBe(2);
    expect(ring().writes).toHaveLength(12); // and nothing was re-coloured to do it
  });
});

describe("sizing the picture in seconds", () => {
  it("keeps three minutes at one row a second", () => {
    const stream = watching();

    run(stream, 8, 1);

    expect(ring().canvas.height).toBe(180);
  });

  it("keeps three minutes at ten rows a second too", () => {
    // The bug this replaces: a constant 180 rows, documented as three minutes, which at
    // 10 fps is eighteen seconds of history.
    const stream = watching();

    run(stream, 8, 0.1);

    expect(ring().canvas.height).toBe(1800);
  });
});

describe("holding the colour window", () => {
  it("is still open after sixteen rows of a ten-a-second stream", () => {
    // Sixteen rows is 1.6 s there — inside the settling window after a retune, and the
    // window frozen on it would paint the whole session wrong, silently. A window still
    // being taken shows as a full repaint: every row already drawn has to be re-coloured.
    const stream = watching();

    run(stream, 16, 0.1);

    expect(ring().writes.at(-1)?.height).toBe(ring().canvas.height);
  });

  it("is held after sixteen rows of a one-a-second stream", () => {
    // The same sixteen rows are sixteen seconds here, so the window was taken and frozen
    // and the sixteenth row is a row of work rather than a repaint.
    const stream = watching();

    run(stream, 16, 1);

    expect(ring().writes.at(-1)?.height).toBe(1);
  });
});

describe("noticing a retune, and not noticing anything else", () => {
  it("keeps the picture through a hertz of readback jitter", () => {
    // The I/Q engine reads the achieved sample rate back off the hardware, so a derived
    // start_hz can flap by a hertz. Blanking on that is a waterfall that never draws.
    const stream = watching();
    const at = run(stream, 16, 1);

    send(stream, at, { start_hz: 144_000_001 });

    expect(ring().writes.at(-1)?.height).toBe(1); // appended, so the history survived
    expect(screen.getByText("144.000")).toBeInTheDocument();
  });

  it("blanks the picture on a real retune", () => {
    // A new band is a new noise floor: holding the old window paints the whole thing one
    // flat colour, and holding the old history draws another frequency under it.
    const stream = watching();
    const at = run(stream, 16, 1);

    send(stream, at, { start_hz: 440_000_000 });

    expect(ring().writes.at(-1)?.height).toBe(ring().canvas.height);
    expect(screen.getByText("440.000")).toBeInTheDocument();
  });
});

describe("what the note claims", () => {
  it("says nothing about the rate until the rows have shown one", () => {
    const stream = watching();

    send(stream, 1000);

    expect(note()).toBe("4 bins of 25.0 kHz");
  });

  it("says one a second on the rtl_power tier", () => {
    const stream = watching();

    run(stream, 8, 1);

    expect(note()).toContain("one row a second");
  });

  it("says ten a second on the I/Q tier", () => {
    // It used to say "one row a second" as a constant, which the fast tier makes a lie.
    const stream = watching();

    run(stream, 8, 0.1);

    expect(note()).toContain("10 rows a second");
  });
});
