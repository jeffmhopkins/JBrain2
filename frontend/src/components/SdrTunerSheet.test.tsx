// The tuner sheet against its binding spec (docs/mocks/sdr-tuner/a-tuner-sheet.html).
// The properties worth pinning are the ones a redesign could quietly lose: that it
// composes the shared Sheet, that Release actually hands the tuner back, and that a
// retune carries the session id so it cannot move someone else's radio.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { SdrListening } from "../sdrSession";
import { SdrTunerSheet } from "./SdrTunerSheet";

const LISTENING: SdrListening = {
  session_id: "abc123",
  frequency_hz: 99_300_000,
  mode: "wbfm",
  gain: null,
  elapsed_s: 72,
  peak: 0.42,
  listeners: 1,
};

afterEach(() => vi.restoreAllMocks());

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
    expect(await screen.findByText("This radio tunes 24-1766 MHz.")).toBeInTheDocument();
    expect(tune).not.toHaveBeenCalled();
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

    // Release is what hands the single tuner back — and what makes the omnibox
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
