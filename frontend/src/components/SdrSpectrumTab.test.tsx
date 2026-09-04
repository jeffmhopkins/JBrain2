// The spectrum tab: what it does with the radio, and what it says when it cannot have
// one. Both are things the owner meets as a blank picture unless the screen says
// otherwise — which is the failure this whole family of surfaces is written against.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { resetBands } from "../sdrBands";
import { type SdrListening, resetSdrSession } from "../sdrSession";
import { resetSdrSpectrum } from "../sdrSpectrum";
import { SdrSpectrumTab } from "./SdrSpectrumTab";

const BANDS = {
  region: "us",
  tuner_min_hz: 100_000,
  tuner_max_hz: 1_766_000_000,
  direct_max_hz: 24_000_000,
  sections: [
    {
      id: "fm-broadcast",
      band: "FM broadcast",
      name: "The dial",
      start_hz: 88_000_000,
      stop_hz: 108_000_000,
      mode: "wbfm",
      step_hz: 200_000,
      channel_hz: 200_000,
      note: "Commercial FM.",
      live: "slow",
      continuous: true,
      sweep_seconds: 60,
      span_hz: 20_000_000,
      centre_hz: 98_000_000,
      hops: 8,
      duty: 0.1,
      surveyable: true,
      direct_sampling: false,
      mirrored: false,
      channels: [],
    },
    {
      id: "49m",
      band: "Shortwave",
      name: "49 m",
      start_hz: 5_900_000,
      stop_hz: 6_200_000,
      mode: "am",
      step_hz: 5_000,
      channel_hz: 5_000,
      note: "International broadcast.",
      live: "none",
      continuous: false,
      sweep_seconds: 120,
      span_hz: 300_000,
      centre_hz: 6_050_000,
      hops: 1,
      duty: 1,
      surveyable: false,
      direct_sampling: true,
      mirrored: false,
      channels: [],
    },
  ],
};

function watching(): SdrListening {
  return {
    session_id: "s1",
    frequency_hz: 98_000_000,
    mode: "fm",
    gain: null,
    purpose: "spectrum",
    sweep: { start_hz: 88_000_000, stop_hz: 108_000_000, bin_hz: 25_000, seconds: 60 },
    started_at: 0,
    elapsed_s: 12,
    peak: 0,
    listeners: 0,
  };
}

beforeEach(() => {
  vi.spyOn(api, "getSdrBands").mockResolvedValue(BANDS as never);
  vi.stubGlobal(
    "EventSource",
    class {
      close(): void {}
    },
  );
});

afterEach(() => {
  resetBands();
  resetSdrSession();
  resetSdrSpectrum();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function idle() {
  vi.spyOn(api, "getSdrStatus").mockResolvedValue({
    available: true,
    listening: null,
    sessions: [],
  } as never);
}

function live() {
  const session = watching();
  vi.spyOn(api, "getSdrStatus").mockResolvedValue({
    available: true,
    listening: session,
    sessions: [session],
  } as never);
}

describe("with nothing being watched", () => {
  it("offers the band picker rather than an empty canvas", async () => {
    idle();

    render(<SdrSpectrumTab />);

    expect(await screen.findByRole("button", { name: "Choose a band" })).toBeTruthy();
  });

  it("starts a picture on the band the owner picked", async () => {
    idle();
    const start = vi.spyOn(api, "sdrSpectrumStart").mockResolvedValue(watching() as never);

    render(<SdrSpectrumTab />);
    fireEvent.click(await screen.findByRole("button", { name: "Choose a band" }));
    fireEvent.click(await screen.findByText(/FM broadcast · The dial/));

    expect(start).toHaveBeenCalledWith({ section: "fm-broadcast" });
  });

  it("will not offer a band the sweep tool cannot reach", async () => {
    // Shortwave listens perfectly and cannot be drawn — rtl_power hardcodes the ADC
    // branch this hardware does not wire. Offering the tap and answering with a 400 is
    // strictly worse than a disabled row that says why.
    idle();

    render(<SdrSpectrumTab />);
    fireEvent.click(await screen.findByRole("button", { name: "Choose a band" }));
    const row = (await screen.findByText(/Shortwave · 49 m/)).closest("button");

    expect(row?.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/cannot be swept/)).toBeTruthy();
  });

  it("says which job has the radio when the box refuses", async () => {
    idle();
    vi.spyOn(api, "sdrSpectrumStart").mockRejectedValue(
      new ApiError(409, "The radio is logging APRS, not watching the spectrum"),
    );

    render(<SdrSpectrumTab />);
    fireEvent.click(await screen.findByRole("button", { name: "Choose a band" }));
    fireEvent.click(await screen.findByText(/FM broadcast · The dial/));

    expect(await screen.findByRole("alert")).toHaveTextContent(/logging APRS/);
  });
});

describe("while watching", () => {
  it("names the band and how honest the picture is", async () => {
    // Eight hops means each frequency is watched about a tenth of each second, and a
    // burst can fall between visits. A waterfall that hid that would look identical to
    // one that could not miss anything.
    live();

    render(<SdrSpectrumTab />);

    expect(await screen.findByText(/FM broadcast · The dial/)).toBeTruthy();
    expect(screen.getByText(/8 hops/)).toBeTruthy();
  });

  it("moves the picture instead of restarting it", async () => {
    // Stop-then-start hands the dongle to whatever asks next, and the owner's waterfall
    // vanishes because they changed band.
    live();
    const tune = vi.spyOn(api, "sdrSpectrumTune").mockResolvedValue(watching() as never);
    const stop = vi.spyOn(api, "sdrStop").mockResolvedValue(undefined as never);

    render(<SdrSpectrumTab />);
    // The band button on the surface, then the row inside the sheet. Both carry the
    // band's name, so the second lookup is scoped — otherwise it finds the button again.
    fireEvent.click(await screen.findByText(/FM broadcast · The dial/));
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText(/FM broadcast · The dial/));

    await waitFor(() => expect(tune).toHaveBeenCalledWith({ section: "fm-broadcast" }, "s1"));
    expect(stop).not.toHaveBeenCalled();
  });

  it("releases the radio on purpose, never by leaving", async () => {
    live();
    const stop = vi.spyOn(api, "sdrStop").mockResolvedValue(undefined as never);

    const view = render(<SdrSpectrumTab />);
    view.unmount();
    expect(stop).not.toHaveBeenCalled();

    render(<SdrSpectrumTab />);
    fireEvent.click(await screen.findByRole("button", { name: "Stop watching" }));

    expect(stop).toHaveBeenCalledWith("s1");
  });
});
