// The spectrum store: what it accepts off the wire, and when it decides the picture
// moved. Both matter because both fail quietly — a row silently dropped is a gap in a
// waterfall nobody can distinguish from a quiet second, and a retune not noticed paints
// the new band with the old band's history under it.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type SpectrumRow,
  parseRow,
  resetSdrSpectrum,
  sameBand,
  sdrSpectrum,
  startSdrSpectrum,
  stopSdrSpectrum,
  subscribeSdrSpectrum,
} from "./sdrSpectrum";

afterEach(() => {
  resetSdrSpectrum();
  vi.unstubAllGlobals();
});

function frame(extra: Record<string, unknown> = {}): string {
  return JSON.stringify({
    at: 100,
    start_hz: 144_000_000,
    stop_hz: 144_050_000,
    bin_hz: 25_000,
    bins: 2,
    db: [-70, -60],
    ...extra,
  });
}

describe("reading a row", () => {
  it("takes the band off the row itself", () => {
    const row = parseRow(frame());

    expect(row).toEqual({
      at: 100,
      startHz: 144_000_000,
      stopHz: 144_050_000,
      binHz: 25_000,
      db: [-70, -60],
      // Empty rather than absent: a quiet band and an older box look the same on the
      // wire, and nothing downstream should have to tell them apart.
      peaks: [],
    });
  });

  it("derives the top edge from the array, not from what the row claims", () => {
    // The renderer places bin i at start + i * bin, so the two have to agree by
    // construction. A stop copied from the payload can disagree with the array it
    // describes, and the picture is then drawn against a frequency axis that lies.
    const row = parseRow(frame({ stop_hz: 999_000_000 }));

    expect(row).toMatchObject({ stopHz: 144_050_000 });
  });

  it("keeps an error apart from a row", () => {
    // The sidecar's own sentence, which names the job holding the radio. Parsed as a
    // row it would be a blank picture with nothing on screen to say why.
    expect(parseRow('{"error":"the radio is logging APRS"}')).toEqual({
      error: "the radio is logging APRS",
    });
  });

  it("skips a keepalive, a torn frame and a row with no numbers", () => {
    // This is text a radio wrote while it was still writing. One unreadable row must
    // cost one row, never the picture.
    expect(parseRow('{"keepalive":true}')).toBeNull();
    expect(parseRow('{"start_hz":144000000,"bin_hz":250')).toBeNull();
    expect(parseRow(frame({ db: [] }))).toBeNull();
    expect(parseRow(frame({ bin_hz: 0 }))).toBeNull();
  });

  it("keeps a bin the radio never wrote as a gap", () => {
    const row = parseRow(frame({ db: [-70, null] }));

    expect(row && "db" in row && Number.isNaN(row.db[1])).toBe(true);
  });
});

function rowOf(raw: string): SpectrumRow | null {
  const parsed = parseRow(raw);
  return parsed && "db" in parsed ? parsed : null;
}

describe("noticing that the picture moved", () => {
  const here = rowOf(frame());

  it("is the same band only when everything about the axis matches", () => {
    expect(sameBand(here, rowOf(frame({ db: [-1, -2] })))).toBe(true);
    expect(sameBand(here, rowOf(frame({ start_hz: 440_000_000 })))).toBe(false);
    expect(sameBand(here, rowOf(frame({ bin_hz: 5_000 })))).toBe(false);
  });

  it("takes a frame that lost a block as the same band, arriving short", () => {
    // `Stitch._flush` emits a short frame ON PURPOSE: the frame width is learned, so a
    // section missing one of its hops is emitted at the timestamp change rather than
    // stalling the picture. The axis is `startHz + i * binHz`, and both survived — every
    // column that did arrive is the frequency it was — which is why `paint` already
    // draws a short row and leaves the rest transparent. Calling it a retune was the
    // expensive half: on an eight-hop band one lost block blanked the history and threw
    // away a colour scale that had taken eighty rows to earn, several times a minute.
    expect(sameBand(here, rowOf(frame({ db: [-1] })))).toBe(true);
  });

  it("is never true of nothing", () => {
    expect(sameBand(null, here)).toBe(false);
    expect(sameBand(here, null)).toBe(false);
  });

  it("does not call a hertz of readback jitter a retune", () => {
    // The I/Q engine reads the ACHIEVED sample rate back off the hardware rather than
    // assuming the requested one, so a derived start_hz can flap by a hertz between
    // frames. Under exact equality that is a retune ten times a second: history blanked
    // and colour scale thrown away every frame, so the picture never draws anything.
    expect(sameBand(here, rowOf(frame({ start_hz: 144_000_001 })))).toBe(true);
    expect(sameBand(here, rowOf(frame({ start_hz: 143_999_999 })))).toBe(true);
  });

  it("does not call a bin width a fraction off a retune either", () => {
    // `bin_hz = rate / N` off a rate that came back 2,047,999 instead of 2,048,000.
    expect(sameBand(here, rowOf(frame({ bin_hz: 24_999.99 })))).toBe(true);
  });

  it("still notices a move the picture could actually draw", () => {
    // Half a bin is the threshold because that is the resolution the picture HAS. A
    // whole bin is a column, and a column is a shift someone can see.
    expect(sameBand(here, rowOf(frame({ start_hz: 144_025_000 })))).toBe(false);
  });

  it("notices a widened span even when the bottom edge did not move", () => {
    // A bin width that really changed moves every column but the first, so the width is
    // checked across the row rather than only the edge a retune happens to move.
    expect(sameBand(here, rowOf(frame({ bin_hz: 40_000 })))).toBe(false);
    // ...including when the wider row is also SHORTER, which is the case a length test
    // would have caught for the wrong reason and this one catches for the right one.
    expect(sameBand(here, rowOf(frame({ bin_hz: 40_000, db: [-1] })))).toBe(false);
  });
});

class FakeSource {
  static last: FakeSource | null = null;
  static readonly CLOSED = 2;
  readyState = 1;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSource.last = this;
  }

  close(): void {
    this.closed = true;
  }
}

function openStream(): FakeSource {
  vi.stubGlobal("EventSource", FakeSource);
  startSdrSpectrum();
  const stream = FakeSource.last;
  if (!stream) throw new Error("no stream opened");
  return stream;
}

describe("the stream", () => {
  it("hands each row to subscribers as it lands", () => {
    const stream = openStream();
    const seen: number[] = [];
    subscribeSdrSpectrum((_state, row) => {
      if (row) seen.push(row.db.length);
    });

    stream.onmessage?.(new MessageEvent("message", { data: frame() }));
    stream.onmessage?.(new MessageEvent("message", { data: frame({ db: [-1, -2, -3] }) }));

    // Handed over directly rather than read back off the state, so a canvas draws
    // exactly the rows that arrived — never one twice, never a skipped one.
    expect(seen).toEqual([2, 3]);
    expect(sdrSpectrum().rows).toBe(2);
  });

  it("does not count a keepalive as a row", () => {
    const stream = openStream();

    stream.onmessage?.(new MessageEvent("message", { data: '{"keepalive":true}' }));

    expect(sdrSpectrum().rows).toBe(0);
    expect(sdrSpectrum().latest).toBeNull();
  });

  it("surfaces the box's own refusal", () => {
    const stream = openStream();

    stream.onmessage?.(
      new MessageEvent("message", { data: '{"error":"the radio is logging APRS"}' }),
    );

    expect(sdrSpectrum().error).toBe("the radio is logging APRS");
  });

  it("says nothing about a blip and something about a closed stream", () => {
    // EventSource reconnects on its own, so a dropped socket is not worth a message.
    const stream = openStream();

    stream.onerror?.();
    expect(sdrSpectrum().error).toBeNull();

    stream.readyState = FakeSource.CLOSED;
    stream.onerror?.();
    expect(sdrSpectrum().error).not.toBeNull();
  });

  it("closes the socket when the picture does", () => {
    const stream = openStream();

    stopSdrSpectrum();

    expect(stream.closed).toBe(true);
    expect(sdrSpectrum().on).toBe(false);
  });

  it("opens one socket however many times it is asked", () => {
    const stream = openStream();
    startSdrSpectrum();

    expect(FakeSource.last).toBe(stream);
  });
});

describe("the signals a row found", () => {
  it("carries them in reading order, renamed for this side of the wire", () => {
    // Found on the BOX, not here: the agent's tools read the same frames, so "what is
    // on the air" cannot have two answers depending on who asked.
    const row = parseRow(
      frame({
        peaks: [
          { hz: 144_390_000, db: -50.2, over_db: 18.4 },
          { hz: 145_000_000, db: -61, over_db: 9 },
        ],
      }),
    );

    expect(row).toMatchObject({
      peaks: [
        { hz: 144_390_000, db: -50.2, overDb: 18.4 },
        { hz: 145_000_000, db: -61, overDb: 9 },
      ],
    });
  });

  it("drops an entry that is not a measurement rather than losing the row", () => {
    // A marker drawn at a frequency nothing was measured at is worse than no marker,
    // and one bad entry must not cost the picture the whole row.
    const row = parseRow(
      frame({ peaks: [{ hz: "loud", db: -50 }, null, { hz: 144_390_000, db: -50 }] }),
    );

    expect(row).toMatchObject({ peaks: [{ hz: 144_390_000, db: -50, overDb: 0 }] });
  });

  it("reads a row from a box that does not report them at all", () => {
    expect(parseRow(frame())).toMatchObject({ peaks: [] });
  });
});
