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

    // The id is what stops a stale client moving a session that has been replaced.
    await waitFor(() => expect(tune).toHaveBeenCalledWith(99.325, undefined, "abc123"));
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
