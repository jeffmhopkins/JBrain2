// The tuner sheet against its binding spec (docs/mocks/sdr-tuner/a-tuner-sheet.html).
// The properties worth pinning are the ones a redesign could quietly lose: that it
// composes the shared Sheet, that Release actually hands the tuner back, and that a
// retune carries the session id so it cannot move someone else's radio.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { resetSdrCaptions } from "../sdrCaptions";
import type { SdrListening } from "../sdrSession";
import { SdrTunerSheet } from "./SdrTunerSheet";

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
  peak: 0.42,
  listeners: 1,
};

afterEach(() => {
  vi.restoreAllMocks();
  resetSdrCaptions();
});

describe("the tuner sheet", () => {
  it("shows the tuned frequency, mode and elapsed time", () => {
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    expect(screen.getByText("99.300")).toBeInTheDocument();
    expect(screen.getByText("1:12")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Tuned station" })).toBeInTheDocument();
  });

  it("tunes by the step and carries the session id", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    // wbfm opens on the 100 kHz step, so broadcast FM is one tap per channel rather
    // than the eight a fixed 25 kHz cost. The id is what stops a stale client moving
    // a session that has been replaced.
    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.4, undefined, "abc123"));
  });

  it("opens on a step that suits the mode", () => {
    const { unmount } = render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: /Tuning step, 100 kHz/ })).toBeInTheDocument();
    unmount();

    // Narrowband voice sits on a raster a 100 kHz step would jump straight over, so
    // the default follows the mode rather than being one value for every band.
    render(<SdrTunerSheet listening={{ ...LISTENING, mode: "fm" }} onClose={() => {}} />);
    expect(screen.getByRole("button", { name: /Tuning step, 25 kHz/ })).toBeInTheDocument();
  });

  it("lets the owner pick the step, and then tunes by it", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

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
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "162.55" } });
    fireEvent.keyDown(field, { key: "Enter" });

    // Stepping reaches a neighbour; typing is how you leave the band entirely.
    await waitFor(() => expect(tune).toHaveBeenCalledWith(162.55, undefined, "abc123"));
  });

  it("refuses a frequency the radio cannot reach, without calling the radio", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

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
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

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
      <SdrTunerSheet
        listening={{ ...LISTENING, frequency_hz: 14_395_000, mode: "am" }}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Tune up" }));

    expect(await screen.findByText(/Nothing between 14.4 and 24 MHz/)).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("still steps freely on the honest side of the line", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(
      <SdrTunerSheet
        listening={{ ...LISTENING, frequency_hz: 14_395_000, mode: "am" }}
        onClose={() => {}}
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
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "9.6" } });
    fireEvent.keyDown(field, { key: "Enter" });

    await waitFor(() => expect(tune).toHaveBeenCalledWith(9.6, undefined, "abc123"));
  });

  it("keeps the frequency field open when something steals focus", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    const { rerender } = render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "93.3" } });
    fireEvent.blur(field);
    // The status poll repaints this sheet once a second while the owner is typing.
    rerender(<SdrTunerSheet listening={{ ...LISTENING, elapsed_s: 73 }} onClose={() => {}} />);

    // Committing on blur meant anything that took focus — a repaint, the keyboard
    // closing — read as the field vanishing mid-entry. The edit ends when the owner
    // says it does, and not before.
    expect(screen.getByRole("textbox", { name: "Frequency in MHz" })).toHaveValue("93.3");
    expect(tune).not.toHaveBeenCalled();
  });

  it("commits the typed frequency from the Go button", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Frequency in MHz" }), {
      target: { value: "93.3" },
    });
    // A number pad does not reliably offer Enter, so the commit has a real control.
    fireEvent.click(screen.getByRole("button", { name: "Tune to this frequency" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(93.3, undefined, "abc123"));
  });

  it("offers play/pause rather than a scrubber over a live stream", () => {
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    // Live radio has no timeline: the native transport rendered a seek bar reading
    // 0:00 / 0:00 forever. It is playing or it is not.
    const transport = screen.getByRole("button", { name: /^(Play|Pause)$/ });
    expect(transport).toBeInTheDocument();
    expect(screen.getByText(/^(LIVE|PAUSED)$/)).toBeInTheDocument();
  });

  it("draws the transport as an icon, not a text glyph", () => {
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

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
    render(<SdrTunerSheet listening={{ ...LISTENING, peak: 0.42 }} onClose={() => {}} />);

    // The meter reported `peak`, which is the loudest sample of the DEMODULATED AUDIO,
    // not reception strength: on an empty FM channel rtl_fm emits loud hiss, so it read
    // high on nothing at all. The tape shows that same quantity honestly and with
    // history, so the meter is gone rather than relabelled.
    expect(screen.queryByText("Signal")).not.toBeInTheDocument();
    expect(screen.queryByText("42%")).not.toBeInTheDocument();
  });

  it("insets the elapsed time on the tape rather than giving it a row", () => {
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    // Layout B: the tape is the panel and the one reading worth keeping sits in the
    // quiet band at its top, costing no height of its own.
    const tape = screen.getByRole("img", { name: /Audio level over the last 12 seconds/ });
    const face = tape.parentElement;
    expect(face).toHaveClass("sdr-face");
    expect(face?.textContent).toContain("1:12");
  });

  it("abandons the edit on Escape without retuning", () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "1" } });
    fireEvent.keyDown(field, { key: "Escape" });

    // Escape must reach the field, not the Sheet: closing the whole tuner over a
    // mistyped digit would be a rude way to lose the edit.
    expect(screen.getByRole("dialog", { name: "Tuned station" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Tap to enter a frequency/ })).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
  });

  it("releases the radio and closes", async () => {
    const stop = vi.spyOn(api, "sdrStop").mockResolvedValue(undefined);
    const onClose = vi.fn();
    render(<SdrTunerSheet listening={LISTENING} onClose={onClose} />);

    fireEvent.click(screen.getByRole("button", { name: "Release" }));

    // Release is what hands this session's radio back — and what makes the omnibox
    // icon disappear, since the icon is the lease.
    await waitFor(() => expect(stop).toHaveBeenCalledWith("abc123"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("surfaces a failure instead of silently doing nothing", async () => {
    vi.spyOn(api, "sdrTune").mockRejectedValue(new Error("The radio is busy."));
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Tune down" }));

    expect(await screen.findByText("The radio is busy.")).toBeInTheDocument();
  });

  it("marks the live mode and switches on tap", async () => {
    const tune = vi.spyOn(api, "sdrTune").mockResolvedValue(LISTENING);
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    expect(screen.getByRole("button", { name: "WBFM" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "AM" }));

    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.3, "am", "abc123"));
  });

  it("does not pretend recording works yet", () => {
    // The binding spec has a Record button; the recording lane is a later wave, so
    // it states that rather than failing on tap.
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    expect(screen.getByRole("button", { name: "Record" })).toBeDisabled();
  });
});

describe("live captions in the tuner", () => {
  it("offers CC off by default, since captions hold a model on the GPU", () => {
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);

    const cc = screen.getByRole("button", { name: "Live captions" });
    expect(cc).toHaveAttribute("aria-pressed", "false");
    // Nothing is burned over the waveform until the owner asks for it.
    expect(screen.queryByText("Listening…")).not.toBeInTheDocument();
  });

  it("burns the caption over the waveform, tinted by confidence", () => {
    vi.useFakeTimers();
    render(<SdrTunerSheet listening={LISTENING} onClose={() => {}} />);
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
