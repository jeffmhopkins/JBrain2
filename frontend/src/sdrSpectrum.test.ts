// The spectrum store: what it accepts off the wire, and when it decides the picture
// moved. Both matter because both fail quietly — a row silently dropped is a gap in a
// waterfall nobody can distinguish from a quiet second, and a retune not noticed paints
// the new band with the old band's history under it.

import { afterEach, describe, expect, it, vi } from "vitest";
import { playSdrAudio, resetSdrAudio } from "./sdrAudio";
import {
  type SpectrumRow,
  parseRow,
  resetSdrSpectrum,
  sameBand,
  scaleLegend,
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
      // Zero rather than absent, for the same reason and with a second job: it is what
      // says this row is a BAND and not a tuned channel, so the tuning view knows not
      // to draw it (sdrTuning.tuningOf).
      passbandHz: 0,
      passbandCentreHz: 0,
      channelHz: 0,
      gainDb: null,
      // False unless the row says the tuner was bypassed — which is what a row from a
      // box that predates the field is, and reading it as "no gain stage" would tell a
      // viewer the levels were absolute when nothing said so.
      tunerBypassed: false,
      // Band unless the row says otherwise, which is what a row from a spectrum session
      // and a row from a box that predates views both are.
      view: "band",
    });
  });

  it("reads the passband that marks a row as a tuned channel", () => {
    // The same stream now carries two kinds of row: a BAND from a spectrum session and
    // a CHANNEL from a listening one, which is the tuning view. A channel row drawn as
    // a band — or the reverse — is a picture of somewhere else with a plausible axis
    // on it.
    expect(parseRow(frame({ passband_hz: 16_000 }))).toMatchObject({ passbandHz: 16_000 });
  });

  // `parseRow` answers with a row, an error, or nothing, so a test that wants the row
  // has to say which of the three it got. `toMatchObject` narrows on its own; a
  // property read does not.
  const viewOf = (raw: string): string | null => {
    const parsed = parseRow(raw);
    return parsed && !("error" in parsed) ? parsed.view : null;
  };

  it("takes the view from the row when the box says which", () => {
    // One session now draws BOTH off one capture, so `passband_hz` has stopped being
    // able to answer this on its own: the band row a listening session publishes is a
    // band row from a session that also has a passband.
    expect(viewOf(frame({ view: "band" }))).toBe("band");
    expect(viewOf(frame({ view: "channel" }))).toBe("channel");
  });

  it("falls back to the passband when the box is older than the field", () => {
    // Exactly the guess every reader used to make, kept for the one case it is still
    // the only available answer.
    expect(viewOf(frame({ passband_hz: 16_000 }))).toBe("channel");
    expect(viewOf(frame())).toBe("band");
    expect(viewOf(frame({ view: "sideways" }))).toBe("band");
  });

  it("believes the row over the passband when the two disagree", () => {
    // A LISTENING session's band row: it has a passband, and it is not a channel.
    expect(viewOf(frame({ view: "band", passband_hz: 16_000 }))).toBe("band");
  });

  it("keeps the SIGN of the passband centre, because the sign is the sideband", () => {
    // C14. SSB is one-sided — `usb` hears +300..+3400 Hz, `lsb` hears -3400..-300 — so
    // a strip shading `passbandHz` centred on the dial covers half the sideband the
    // demodulator REJECTS. Every other number here is clamped at zero; clamping this one
    // would erase `lsb` entirely.
    expect(parseRow(frame({ passband_centre_hz: 1_850 }))).toMatchObject({
      passbandCentreHz: 1_850,
    });
    expect(parseRow(frame({ passband_centre_hz: -1_850 }))).toMatchObject({
      passbandCentreHz: -1_850,
    });
  });

  it("centres the passband on the dial when the box does not say otherwise", () => {
    // Zero is what every symmetric mode sends and what a box older than the field sends,
    // so a strip that reads it draws exactly what it drew before C14.
    expect(parseRow(frame())).toMatchObject({ passbandCentreHz: 0 });
    expect(parseRow(frame({ passband_centre_hz: "left" }))).toMatchObject({
      passbandCentreHz: 0,
    });
    expect(parseRow(frame({ passband_centre_hz: Number.NaN }))).toMatchObject({
      passbandCentreHz: 0,
    });
  });

  it("treats a missing or nonsense passband as a band row", () => {
    // A box older than the demodulator sends none, and a row that has been mangled must
    // not become a channel view centred on nothing.
    expect(parseRow(frame())).toMatchObject({ passbandHz: 0 });
    expect(parseRow(frame({ passband_hz: "wide" }))).toMatchObject({ passbandHz: 0 });
    expect(parseRow(frame({ passband_hz: -5 }))).toMatchObject({ passbandHz: 0 });
    expect(parseRow(frame({ passband_hz: Number.NaN }))).toMatchObject({ passbandHz: 0 });
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

  it("keeps the socket open while another holder still wants it", () => {
    // There is ONE socket for the whole app and the listen controls are mounted twice
    // at once — the omnibox sheet, and the Radios tab that stays mounted behind it.
    // Closing the sheet used to shut the socket for both, and the survivor never
    // noticed: its effect's deps had not changed, so it never reopened and its tuning
    // strip sat frozen on its last row under audio that was still playing.
    vi.stubGlobal("EventSource", FakeSource);
    startSdrSpectrum("all", "sheet");
    startSdrSpectrum("all", "tab");
    const stream = FakeSource.last;
    if (!stream) throw new Error("no stream opened");

    stopSdrSpectrum("sheet");

    expect(stream.closed).toBe(false);
    expect(sdrSpectrum().on).toBe(true);
  });

  it("closes it when the LAST holder goes", () => {
    vi.stubGlobal("EventSource", FakeSource);
    startSdrSpectrum("all", "sheet");
    startSdrSpectrum("all", "tab");
    const stream = FakeSource.last;
    if (!stream) throw new Error("no stream opened");

    stopSdrSpectrum("sheet");
    stopSdrSpectrum("tab");

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

describe("a peak's channel and its measurement", () => {
  it("carries both, so the label can be checked against what was seen", () => {
    // The box snaps a signal to its channel when it can establish the grid; the argmax
    // it actually saw rides alongside. A label nobody can compare against the
    // measurement is exactly the kind of number this project keeps finding.
    const row = parseRow(
      frame({ peaks: [{ hz: 88_100_000, measured_hz: 88_084_400, db: -40, over_db: 22 }] }),
    );

    expect(row).toMatchObject({
      peaks: [{ hz: 88_100_000, measuredHz: 88_084_400, db: -40, overDb: 22 }],
    });
  });

  it("treats a missing measurement as equal to the label", () => {
    // Two cases, same answer: a box older than the snap, and this box on a band where it
    // could not establish a grid. In both the label IS the measurement.
    const row = parseRow(frame({ peaks: [{ hz: 90_300_000, db: -50, over_db: 14 }] }));

    expect(row).toMatchObject({ peaks: [{ hz: 90_300_000, measuredHz: 90_300_000 }] });
  });
});

describe("drawing a row when its audio is heard", () => {
  /** Put the listener's ear at a known point on the box's clock — the same trick
   *  sdrCaptions.test.ts uses, because it is the same alignment. */
  function earAt(boxClock: number, played: number): void {
    playSdrAudio(boxClock);
    const el = document.querySelector("audio");
    if (!el) throw new Error("no audio element");
    Object.defineProperty(el, "paused", { value: false, configurable: true });
    Object.defineProperty(el, "currentTime", { value: played, configurable: true });
  }

  afterEach(() => {
    resetSdrAudio();
    vi.useRealTimers();
  });

  function rows(): number[] {
    const seen: number[] = [];
    subscribeSdrSpectrum((_state, row) => {
      if (row) seen.push(row.at);
    });
    return seen;
  }

  it("holds a row until the speaker reaches it", () => {
    // THE REPORT: "the spectrum and the actual audio out of my speakers are not
    // matched", and named the most important thing to get right. Rows come over SSE at
    // the live edge; the ear is behind it. Both are stamped on the box's own clock, so
    // this closes the gap exactly rather than approximately.
    vi.useFakeTimers();
    earAt(1000, 0); // the speaker is playing the air of one second 1000
    const stream = openStream();
    const seen = rows();

    stream.onmessage?.(new MessageEvent("message", { data: frame({ at: 1004 }) }));
    vi.advanceTimersByTime(500);

    expect(seen).toEqual([]); // four seconds of air the owner has not heard yet
  });

  it("draws it when the audio arrives, and not before", () => {
    vi.useFakeTimers();
    earAt(1000, 0);
    const stream = openStream();
    const seen = rows();
    stream.onmessage?.(new MessageEvent("message", { data: frame({ at: 1004 }) }));
    vi.advanceTimersByTime(500);

    // The listener has now played four seconds, so 1004 is what is coming out.
    const el = document.querySelector("audio") as HTMLAudioElement;
    Object.defineProperty(el, "currentTime", { value: 4, configurable: true });
    vi.advanceTimersByTime(100);

    expect(seen).toEqual([1004]);
  });

  it("draws every row that came due, in order, not just the newest", () => {
    // The difference from captions. Each row is a LINE of the waterfall: dropping the
    // ones that came due together would eat the history the picture exists to show,
    // where an older caption is simply past being worth reading.
    vi.useFakeTimers();
    earAt(1000, 0);
    const stream = openStream();
    const seen = rows();
    for (const at of [1001, 1002, 1003]) {
      stream.onmessage?.(new MessageEvent("message", { data: frame({ at }) }));
    }

    const el = document.querySelector("audio") as HTMLAudioElement;
    Object.defineProperty(el, "currentTime", { value: 3, configurable: true });
    vi.advanceTimersByTime(100);

    expect(seen).toEqual([1001, 1002, 1003]);
  });

  it("draws straight through when there is no audio to match", () => {
    // A spectrum session on the Radio tab is its own purpose on its own radio and makes
    // no sound. Nothing to align with, so nothing is held — and a picture that waited
    // for an ear that does not exist would simply never draw.
    vi.useFakeTimers();
    const stream = openStream();
    const seen = rows();

    stream.onmessage?.(new MessageEvent("message", { data: frame({ at: 5000 }) }));

    expect(seen).toEqual([5000]);
  });

  it("draws straight through while the radio is paused", () => {
    // `sdrHeardAt()` goes on answering from a paused element — `currentTime` just stops
    // — so aligning to it would freeze the waterfall for as long as the owner left the
    // radio paused while watching the picture.
    vi.useFakeTimers();
    earAt(1000, 0);
    const el = document.querySelector("audio") as HTMLAudioElement;
    Object.defineProperty(el, "paused", { value: true, configurable: true });
    const stream = openStream();
    const seen = rows();

    stream.onmessage?.(new MessageEvent("message", { data: frame({ at: 9000 }) }));

    expect(seen).toEqual([9000]);
  });

  it("gives up holding rather than freezing the picture for ever", () => {
    // Both clocks are the box's own and should not drift, but the anchor is taken once
    // when the stream is attached. If it is ever wrong in the slow direction, a late
    // picture is a defect and a frozen one is a broken radio.
    vi.useFakeTimers();
    earAt(1000, 0);
    const stream = openStream();
    const seen = rows();

    stream.onmessage?.(new MessageEvent("message", { data: frame({ at: 1_000_000 }) }));
    vi.advanceTimersByTime(500);
    expect(seen).toEqual([]);

    vi.advanceTimersByTime(21_000);

    expect(seen).toEqual([1_000_000]);
  });
});

describe("which picture, and off which radio", () => {
  it("names the radio and the backfill on the wire", () => {
    vi.stubGlobal("EventSource", FakeSource);

    startSdrSpectrum({ view: "channel", serial: "09022796", backfill: 120 });

    const url = FakeSource.last?.url ?? "";
    expect(url).toContain("view=channel");
    expect(url).toContain("serial=09022796");
    expect(url).toContain("backfill=120");
  });

  it("REOPENS when another surface wants a different radio", () => {
    // The bug the omnibox sheet exposed: two tabs over two radios, one stream. The
    // second tab's ask used to be ignored — "the first caller's view stands" — so it
    // sat waiting for rows of a picture nothing was sending it.
    vi.stubGlobal("EventSource", FakeSource);
    startSdrSpectrum({ view: "band", serial: "AAA" });
    const first = FakeSource.last;

    startSdrSpectrum({ view: "channel", serial: "BBB" });

    expect(first?.closed).toBe(true);
    expect(FakeSource.last).not.toBe(first);
    expect(FakeSource.last?.url).toContain("serial=BBB");
  });

  it("leaves the stream alone when the same picture is asked for again", () => {
    // A re-render must not tear down a running waterfall: every row in flight would be
    // lost and the canvas would blank.
    vi.stubGlobal("EventSource", FakeSource);
    startSdrSpectrum({ view: "band", serial: "AAA", backfill: 120 });
    const first = FakeSource.last;

    startSdrSpectrum({ view: "band", serial: "AAA", backfill: 120 });

    expect(FakeSource.last).toBe(first);
    expect(first?.closed).toBe(false);
  });

  it("still takes a bare view, as every caller before two radios did", () => {
    vi.stubGlobal("EventSource", FakeSource);

    startSdrSpectrum("band");

    expect(FakeSource.last?.url).toContain("view=band");
    expect(FakeSource.last?.url).not.toContain("serial=");
  });
});

describe("what the dB scale means", () => {
  // SAID ON EVERY ROW, not only the suspect ones. A spectrum session pins its gain by
  // construction — a waterfall whose gain moves has a scale that means nothing from one
  // row to the next — and a legend that appeared only when something was wrong would
  // train the reader to stop reading it (docs/mocks/radio-settings/README.md).

  function row(over: Record<string, unknown>): SpectrumRow {
    const parsed = parseRow(frame(over));
    if (parsed === null || "error" in parsed) throw new Error("the fixture did not parse");
    return parsed;
  }

  it("names the gain the picture was drawn at", () => {
    expect(scaleLegend(row({ gain_db: 30 }))).toBe("dBFS @ 30 dB");
  });

  it("calls the scale relative when the gain is moving", () => {
    // Under AGC the absolute level means nothing BETWEEN rows: the same radio on
    // 162.550 grew a station at 162.35 and a spur comb at ±55.5/111/166/222 kHz.
    expect(scaleLegend(row({ gain_db: null }))).toBe("relative — gain is moving");
  });

  it("says there was no gain stage when the tuner was bypassed", () => {
    // Not the same claim as "the gain is moving", and the difference is the whole
    // reason the field exists: on the direct path the levels are true dBFS and there
    // is no gain to state, because every gain stage an R820T2 has is in a tuner that
    // `rtlsdr_set_direct_sampling` powered down.
    expect(scaleLegend(row({ gain_db: null, tuner_bypassed: true }))).toBe("dBFS (no gain stage)");
  });

  it("never reports a gain a bypassed tuner could not have applied", () => {
    // The row carries what the SESSION asked for; a measuring session asks for 30 dB
    // whatever the band. If a bypassed row ever arrives carrying one, the legend must
    // not repeat it as a measurement.
    expect(scaleLegend(row({ gain_db: 30, tuner_bypassed: true }))).toBe("dBFS (no gain stage)");
  });
});
