// The tuner controls against their binding spec (docs/mocks/sdr-tuner/a-tuner-sheet.html).
// The properties worth pinning are the ones a redesign could quietly lose: that Release
// actually hands the tuner back, and that a retune carries the session id so it cannot
// move someone else's radio.
//
// These mount inside a sheet (the omnibox radio sheet) and inside the Radios tab, and
// are rendered bare here — what the sheet around them does is SdrRadiosSheet's test.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { resetBands } from "../sdrBands";
import { resetSdrCaptions } from "../sdrCaptions";
import type { SdrListening } from "../sdrSession";
import { SdrTunerControls, liveTag } from "./SdrTunerControls";

// The caption stream, faked at the EventSource seam so a test can deliver a segment.
class FakeEventSource {
  static last: FakeEventSource | null = null;
  static CLOSED = 2;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
  send(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
}
function captionStream(): FakeEventSource | null {
  return FakeEventSource.last;
}
vi.stubGlobal("EventSource", FakeEventSource);

const LISTENING: SdrListening = {
  session_id: "abc123",
  frequency_hz: 99_300_000,
  mode: "wbfm",
  gain: null,
  started_at: 1_700_000_000,
  elapsed_s: 72,
  audio_peak: 0.42,
  listeners: 1,
};

/** The band table the controls ask for, stubbed at the api seam.
 *
 *  Empty by default and reset between cases: whether a channel plan covers the radio
 *  decides what ± does, so a table left over from another test would change the
 *  meaning of every step below it. */
function bands(sections: unknown[] = []) {
  vi.spyOn(api, "getSdrBands").mockResolvedValue({
    region: "us",
    tuner_min_hz: 100_000,
    tuner_max_hz: 1_766_000_000,
    direct_max_hz: 24_000_000,
    sections,
  } as never);
}

beforeEach(() => {
  bands();
  // jsdom has no layout engine, so scrollIntoView is undefined on Element.
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  resetBands();
  vi.restoreAllMocks();
  resetSdrCaptions();
});

describe("the tuner controls", () => {
  it("shows the tuned frequency, mode and elapsed time", () => {
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    expect(screen.getByText("99.300")).toBeInTheDocument();
    expect(screen.getByText("1:12")).toBeInTheDocument();
  });

  it("tunes by the step and carries the session id", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    // wbfm opens on the 100 kHz step, so broadcast FM is one tap per channel rather
    // than the eight a fixed 25 kHz cost. The id is what stops a stale client moving
    // a session that has been replaced.
    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.4, undefined, "abc123"));
  });

  it("opens on a step that suits the mode", () => {
    const { unmount } = render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);
    expect(screen.getByRole("button", { name: /Tuning step, 100 kHz/ })).toBeInTheDocument();
    unmount();

    // Narrowband voice sits on a raster a 100 kHz step would jump straight over, so
    // the default follows the mode rather than being one value for every band.
    render(<SdrTunerControls listening={{ ...LISTENING, mode: "fm" }} onReleased={() => {}} />);
    expect(screen.getByRole("button", { name: /Tuning step, 25 kHz/ })).toBeInTheDocument();
  });

  it("lets the owner pick the step, and then tunes by it", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tuning step/ }));
    fireEvent.click(screen.getByRole("button", { name: "12.5 kHz", pressed: false }));

    // The picker closes on choice and the chip reports what is now in force.
    expect(screen.queryByRole("button", { name: "12.5 kHz" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Tuning step, 12.5 kHz/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));
    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.3125, undefined, "abc123"));
  });

  it("tunes to a frequency typed into the readout", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "162.55" } });
    fireEvent.keyDown(field, { key: "Enter" });

    // Stepping reaches a neighbour; typing is how you leave the band entirely.
    await waitFor(() => expect(tune).toHaveBeenCalledWith(162.55, undefined, "abc123"));
  });

  it("refuses a frequency the radio cannot reach, without calling the radio", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "5000" } });
    fireEvent.keyDown(field, { key: "Enter" });

    // The message names the range: a bare refusal leaves the owner guessing at it.
    expect(await screen.findByText("This radio tunes 0.1-1766 MHz.")).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("refuses the second Nyquist zone, which lowering the floor exposed", async () => {
    // 14.4-24 MHz is the hole between the two floors. Below 24 the tuner is powered
    // down and the ADC samples at 28.8 MHz, so a request for 18.1 is answered with
    // 10.7, mirrored — a session that reports healthy, a level meter that moves, and a
    // completely different station. There is nothing in the audio to notice it by,
    // which is why this is a refusal and not a warning.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "18.1" } });
    fireEvent.keyDown(field, { key: "Enter" });

    // The number the owner would have been hearing is IN the sentence: "out of range"
    // would be a lie, because the radio tunes it — just not where it says.
    expect(await screen.findByText(/10\.700 MHz instead/)).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("refuses to STEP into that zone as well, one tap at a time", async () => {
    // The steppers reach it too, and a guard on only the typed field is a guard on
    // neither: from 14.395 MHz a single tap of the AM step crosses the line.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(
      <SdrTunerControls
        listening={{ ...LISTENING, frequency_hz: 14_395_000, mode: "am" }}
        onReleased={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    expect(await screen.findByText(/Nothing between 14.4 and 24 MHz/)).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("still steps freely on the honest side of the line", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(
      <SdrTunerControls
        listening={{ ...LISTENING, frequency_hz: 14_395_000, mode: "am" }}
        onReleased={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Tune down" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(14.385, undefined, "abc123"));
  });

  it("lets shortwave through, because the radio reaches it by bypassing the tuner", async () => {
    // This field refused everything under 24 MHz — the R820T2 TUNER's floor, retyped
    // here — while `rtl_fm -E direct2` has been listening down to 100 kHz all along and
    // every route behind it bounds on the radio's real floor. A duplicated bound
    // refusing what the box can do is the bug class `jbrain/sdr/tuner.py` exists to end.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "9.6" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => expect(tune).toHaveBeenCalledWith(9.6, undefined, "abc123"));
  });

  it("keeps the frequency field open when something steals focus", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    const { rerender } = render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "93.3" } });
    fireEvent.blur(field);
    // The status poll repaints this sheet once a second while the owner is typing.
    rerender(
      <SdrTunerControls listening={{ ...LISTENING, elapsed_s: 73 }} onReleased={() => {}} />,
    );

    // Committing on blur meant anything that took focus — a repaint, the keyboard
    // closing — read as the field vanishing mid-entry. The edit ends when the owner
    // says it does, and not before.
    expect(screen.getByRole("textbox", { name: "Frequency in MHz" })).toHaveValue("93.3");
    expect(tune).not.toHaveBeenCalled();
  });

  it("commits the typed frequency from the Go button", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Frequency in MHz" }), {
      target: { value: "93.3" },
    });
    // A number pad does not reliably offer Enter, so the commit has a real control.
    fireEvent.click(screen.getByRole("button", { name: "Tune to this frequency" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(93.3, undefined, "abc123"));
  });

  it("offers play/pause rather than a scrubber over a live stream", () => {
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    // Live radio has no timeline: the native transport rendered a seek bar reading
    // 0:00 / 0:00 forever. It is playing or it is not.
    const transport = screen.getByRole("button", { name: /^(Play|Pause)$/ });
    expect(transport).toBeInTheDocument();
    expect(screen.getByText(/^(LIVE|PAUSED)$/)).toBeInTheDocument();
  });

  it("draws the transport as an icon, not a text glyph", () => {
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    const transport = screen.getByRole("button", { name: /^(Play|Pause)$/ });

    // It rendered "▶"/"❚❚" as characters once. Wherever the platform substituted a font
    // whose side-bearings were not symmetric — iOS did — the triangle sat visibly left
    // of its circle, and no amount of text centring could correct it: the glyph is
    // centred inside its own advance width, and the padding belongs to the font.
    // DESIGN.md "Iconography" bars emoji in chrome for the same reason.
    expect(transport.querySelector("svg")).not.toBeNull();
    expect(transport.textContent).toBe("");
  });

  it("shows no signal meter — the tape is the level display", () => {
    render(
      <SdrTunerControls listening={{ ...LISTENING, audio_peak: 0.42 }} onReleased={() => {}} />,
    );

    // The meter reported `peak`, which is the loudest sample of the DEMODULATED AUDIO,
    // not reception strength: on an empty FM channel rtl_fm emits loud hiss, so it read
    // high on nothing at all. The tape shows that same quantity honestly and with
    // history, so the meter is gone rather than relabelled.
    expect(screen.queryByText("Signal")).not.toBeInTheDocument();
    expect(screen.queryByText("42%")).not.toBeInTheDocument();
  });

  it("insets the elapsed time on the tape rather than giving it a row", () => {
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    // Layout B: the tape is the panel and the one reading worth keeping sits in the
    // quiet band at its top, costing no height of its own.
    const tape = screen.getByRole("img", { name: /Audio level over the last 12 seconds/ });
    const face = tape.parentElement;
    expect(face).toHaveClass("sdr-face");
    expect(face?.textContent).toContain("1:12");
  });

  it("abandons the edit on Escape without retuning", () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "1" } });
    fireEvent.keyDown(field, { key: "Escape" });

    // Escape abandons the EDIT, nothing more. That it does not also close the sheet
    // these controls are mounted in is the sheet's property, tested there.
    expect(screen.getByRole("button", { name: /Tap to enter a frequency/ })).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("releases the radio and closes", async () => {
    const stop = vi.spyOn(api, "sdrStop").mockResolvedValue(undefined);
    const onReleased = vi.fn();
    render(<SdrTunerControls listening={LISTENING} onReleased={onReleased} />);

    fireEvent.click(screen.getByRole("button", { name: "Release" }));

    // Release is what hands this session's radio back — and what makes the omnibox
    // icon disappear, since the icon is the lease.
    await waitFor(() => expect(stop).toHaveBeenCalledWith("abc123"));
    await waitFor(() => expect(onReleased).toHaveBeenCalled());
  });

  it("surfaces a failure instead of silently doing nothing", async () => {
    vi.spyOn(api, "sdrTune").mockRejectedValue(new Error("The radio is busy."));
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Tune down" }));

    expect(await screen.findByText("The radio is busy.")).toBeInTheDocument();
  });

  it("marks the live mode and switches on tap", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    expect(screen.getByRole("button", { name: "WBFM" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "AM" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.3, "am", "abc123"));
  });

  it("does not pretend recording works yet", () => {
    // The binding spec has a Record button; the recording lane is a later wave, so
    // it states that rather than failing on tap.
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();
  });
});

describe("live captions in the tuner", () => {
  it("offers CC off by default, since captions hold a model on the GPU", () => {
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    const cc = screen.getByRole("button", { name: "Live captions" });
    expect(cc).toHaveAttribute("aria-pressed", "false");
    // Nothing is burned over the waveform until the owner asks for it.
    expect(screen.queryByText("Listening…")).not.toBeInTheDocument();
  });

  it("burns the caption over the waveform, tinted by confidence", () => {
    vi.useFakeTimers();
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Live captions" }));

    act(() => {
      captionStream()?.send({
        started_at: 4,
        text: "winds south southeast",
        words: [
          { text: "winds", confidence: 0.95 },
          { text: "southeast", confidence: 0.4 },
        ],
      });
    });
    // Captions are held until their audio is heard; with no anchored timeline here
    // the release tick shows them anyway (sdrCaptions.ts).
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // A confident word and a shaky one must not render the same: the colour is the
    // whole reason the words carry confidence at all.
    const sure = screen.getByText("winds", { exact: false });
    const shaky = screen.getByText("southeast", { exact: false });
    expect(sure.getAttribute("style")).not.toBe(shaky.getAttribute("style"));
    // The caption sits on the tape's face, not in a row of its own.
    expect(sure.closest(".sdr-face")).not.toBeNull();
    vi.useRealTimers();
  });
});

describe("what the transport calls live", () => {
  it("says LIVE when the speaker is near the air", () => {
    expect(liveTag(0)).toBe("LIVE");
    expect(liveTag(1.4)).toBe("LIVE");
  });

  it("says how far behind once it is behind", () => {
    // The tag read LIVE straight through the eight seconds the element ran behind the
    // air — a label naming a quantity it never measured, which is the recurring defect
    // in this subsystem rather than a one-off.
    expect(liveTag(8.3)).toBe("LIVE −8s");
    expect(liveTag(2)).toBe("LIVE −2s");
  });

  it("says LIVE when there is nothing to measure", () => {
    // No element, or one with no buffered range yet: an unknown delay is not a claim
    // that the radio is late.
    expect(liveTag(null)).toBe("LIVE");
  });
});

describe("the mode row", () => {
  it("offers every demodulator the back end has, LSB included", () => {
    // LSB was missing while the sidecar has always had it, and the 40 m and 80 m
    // sections SELECT it — so the radio arrived in a mode whose button did not exist,
    // reading "LSB" under the readout above a row of four it was not one of.
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    const row = screen.getByRole("group", { name: "Demodulation mode" });
    expect([...row.querySelectorAll("button")].map((b) => b.textContent)).toEqual([
      "WBFM",
      "FM",
      "AM",
      "USB",
      "LSB",
    ]);
  });

  it("puts the radio on the sideband that was missing", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerControls listening={LISTENING} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "LSB" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.3, "lsb", "abc123"));
  });

  it("shows the mode the radio is in as the selected one", () => {
    render(<SdrTunerControls listening={{ ...LISTENING, mode: "lsb" }} onReleased={() => {}} />);

    expect(screen.getByRole("button", { name: "LSB" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("tuning finer than a kilohertz", () => {
  it("opens SSB on 100 Hz rather than the 25 kHz fallback", () => {
    // Measured on air at 7.305 LSB: the pill read 25 KHZ, which on a mode with no
    // carrier does not move you beside the signal — it moves the voice 25 kHz.
    render(<SdrTunerControls listening={{ ...LISTENING, mode: "lsb" }} onReleased={() => {}} />);

    expect(screen.getByRole("button", { name: /Tuning step, 100 Hz/ })).toBeInTheDocument();
  });

  it("tunes by a sub-kilohertz step", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    const onAir: SdrListening = { ...LISTENING, frequency_hz: 7_305_000, mode: "lsb" };
    render(<SdrTunerControls listening={onAir} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tuning step/ }));
    fireEvent.click(screen.getByRole("button", { name: "10 Hz", pressed: false }));
    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    // Six decimal places of MHz is 1 Hz, so a 10 Hz step survives the round trip.
    await waitFor(() => expect(tune).toHaveBeenCalledWith(7.30501, undefined, "abc123"));
  });

  it("labels sub-kilohertz steps in hertz, not as a fraction of a kilohertz", () => {
    render(<SdrTunerControls listening={{ ...LISTENING, mode: "lsb" }} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tuning step/ }));

    // "0.1 kHz" is a step size nobody says out loud, and the leading zero is the digit
    // that gets misread on a dial.
    expect(screen.getByRole("button", { name: "100 Hz" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "0.1 kHz" })).not.toBeInTheDocument();
  });
});

describe("counting in channels", () => {
  // CB, cut down to the awkward part: 23 sits ABOVE 24 and 25, so channel order and
  // frequency order genuinely differ and a step size cannot reproduce the dial.
  const CB = {
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
      { hz: 27_215_000, name: "Ch 21", note: "" },
      { hz: 27_225_000, name: "Ch 22", note: "" },
      { hz: 27_255_000, name: "Ch 23", note: "" },
      { hz: 27_235_000, name: "Ch 24", note: "" },
    ],
  };
  const ON_22: SdrListening = { ...LISTENING, frequency_hz: 27_225_000, mode: "am" };

  it("names the channel on the pill instead of a step size", async () => {
    bands([CB]);
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    expect(
      await screen.findByRole("button", { name: /Ch 22 of Citizens band/ }),
    ).toBeInTheDocument();
  });

  it("steps to the NEXT CHANNEL, not the next 10 kHz", async () => {
    // The case the whole feature exists for: 22 → 23 is +30 kHz, over the top of 24.
    // A 10 kHz step lands on 27.235, which every other CB radio in earshot calls 24.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(ON_22);
    bands([CB]);
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    await screen.findByRole("button", { name: /Ch 22 of/ });
    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(27.255, undefined, "abc123"));
  });

  it("says so at the end of the plan rather than walking out of the band", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(ON_22);
    bands([CB]);
    render(
      <SdrTunerControls listening={{ ...ON_22, frequency_hz: 27_235_000 }} onReleased={() => {}} />,
    );

    await screen.findByRole("button", { name: /Ch 24 of/ });
    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    expect(await screen.findByText(/top of Citizens band/)).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("opens the channel list and tunes straight to one", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(ON_22);
    bands([CB]);
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /Ch 22 of/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Ch 23, 27\.255 MHz/ }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(27.255, undefined, "abc123"));
  });

  it("scrolls the tuned channel into view when the list opens", async () => {
    // The AM dial is 118 channels and would open at 530 kHz. A list the owner has to
    // search for their own channel in is a list that costs more than the ± it replaced.
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    bands([CB]);
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /Ch 22 of/ }));

    expect(scrollIntoView).toHaveBeenCalled();
  });

  it("does NOT re-scroll the list while the owner is scrolling it", async () => {
    // The regression this pins: the ref was an inline arrow, so React detached and
    // re-attached it on EVERY render — and the tuner re-renders about once a second off
    // the live poll. On the 100-channel FM dial the list snapped back to the tuned
    // channel every second, which is exactly when a long list is least usable.
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    bands([CB]);
    const { rerender } = render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /Ch 22 of/ }));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    // A poll tick: same radio, same channel, one more second on the clock.
    rerender(<SdrTunerControls listening={{ ...ON_22, elapsed_s: 73 }} onReleased={() => {}} />);
    rerender(<SdrTunerControls listening={{ ...ON_22, elapsed_s: 74 }} onReleased={() => {}} />);

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("re-centres when the RADIO moves to another channel", async () => {
    // The other half of the same rule: a poll must not scroll, but retuning must — the
    // list is showing where the radio is, so it follows the radio.
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    bands([CB]);
    const { rerender } = render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /Ch 22 of/ }));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    rerender(
      <SdrTunerControls listening={{ ...ON_22, frequency_hz: 27_255_000 }} onReleased={() => {}} />,
    );

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });

  it("says Off channel between channels, and snaps on the next tap", async () => {
    // A typed frequency. Naming the nearest channel would claim the radio is somewhere
    // it is not, on the one control whose job is saying where it is.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(ON_22);
    bands([CB]);
    render(
      <SdrTunerControls listening={{ ...ON_22, frequency_hz: 27_230_000 }} onReleased={() => {}} />,
    );

    expect(await screen.findByRole("button", { name: /Off channel of/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(27.235, undefined, "abc123"));
  });

  it("hands the kilohertz step back when the owner asks for it", async () => {
    // Free tuning is never taken away: on CB the interesting thing is sometimes
    // between two channels, and a plan that could not be left would be a cage.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(ON_22);
    bands([CB]);
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /Ch 22 of/ }));
    fireEvent.click(screen.getByRole("button", { name: /kHz step/ }));
    fireEvent.click(screen.getByRole("button", { name: "10 kHz" }));
    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(27.235, undefined, "abc123"));
  });

  it("leaves the dial counting kilohertz where no plan covers the radio", async () => {
    // Airband is allocated per facility, so its named channels are landmarks. The
    // table arriving must not change what ± does on a band it says nothing about.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    bands([{ ...CB, id: "air", name: "Tower", channel_plan: false }]);
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Tuning step/ })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(27.235, undefined, "abc123"));
  });

  it("still counts kilohertz when the band table cannot be read", async () => {
    // Best-effort: a table that fails to load leaves the dial exactly as it was,
    // rather than a ± that does nothing on a radio that is plainly tuned.
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    vi.spyOn(api, "getSdrBands").mockRejectedValue(new Error("nope"));
    render(<SdrTunerControls listening={ON_22} onReleased={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(27.235, undefined, "abc123"));
  });
});
