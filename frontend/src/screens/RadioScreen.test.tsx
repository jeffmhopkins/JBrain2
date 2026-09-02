// The Radio launcher's APRS tab.
//
// Three properties worth pinning, all of which a refactor could quietly lose: that a
// dead receiver never reads as a quiet channel, that heard packets are rendered as
// somebody else's transmission rather than as content of ours, and that the Tuner tab
// says when APRS is holding the radio and offers the handoff back.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { STALE_AFTER_MS } from "../aprsLog";
import { resetSdrSession } from "../sdrSession";
import { RadioScreen } from "./RadioScreen";

const PACKET = {
  heard_at: new Date().toISOString(),
  frequency_hz: 144_390_000,
  source: "KE8XYZ-9",
  destination: "APDW17",
  path: ["WIDE1-1"],
  info: "GATE 7K2M9",
};

function log(over: Record<string, unknown> = {}) {
  return { logging: true, frequency_hz: 144_390_000, packets: [PACKET], ...over };
}

afterEach(() => {
  resetSdrSession();
  vi.restoreAllMocks();
});

describe("the APRS tab", () => {
  it("shows what was heard", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText("GATE 7K2M9")).toBeInTheDocument();
    expect(screen.getByText("KE8XYZ-9")).toBeInTheDocument();
  });

  it("badges a packet as heard rather than presenting it as ours", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });

    render(<RadioScreen onClose={() => {}} />);

    // A packet is a stranger's transmission with a forgeable callsign. The badge is
    // where that rule stops being a line in a plan and meets the owner's eye.
    await screen.findByText("GATE 7K2M9");
    expect(screen.getByText("heard")).toBeInTheDocument();
  });

  it("does not read a silent receiver as a quiet channel", async () => {
    const stale = {
      ...PACKET,
      heard_at: new Date(Date.now() - STALE_AFTER_MS - 60_000).toISOString(),
    };
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log({ packets: [stale] }) as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/nothing for \d+ min/)).toBeInTheDocument();
  });

  it("offers the switch when logging is off, and says what it costs", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const set = vi.spyOn(api, "setAprsLogging").mockResolvedValue({ logging: true, changed: true });

    render(<RadioScreen onClose={() => {}} />);

    const button = await screen.findByRole("button", { name: "Enable APRS logging" });
    // The cost is named where the switch is, not in a hint read earlier.
    expect(screen.getByText(/Reserves the tuner until released/)).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(set).toHaveBeenCalledWith(true));
  });

  it("surfaces which job holds the radio when the switch is refused", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const { ApiError } = await import("../api/client");
    vi.spyOn(api, "setAprsLogging").mockRejectedValue(
      new ApiError(409, "The radio is already listening"),
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Enable APRS logging" }));

    // Generic failure would leave the owner with no next move; the sidecar names the
    // holder for exactly that reason and it must survive all the way to the screen.
    expect(await screen.findByText(/already listening/)).toBeInTheDocument();
  });
});

describe("the Tuner tab", () => {
  it("says when APRS is holding the radio, and offers it back", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const set = vi
      .spyOn(api, "setAprsLogging")
      .mockResolvedValue({ logging: false, changed: true });

    render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("GATE 7K2M9");
    fireEvent.click(screen.getByRole("tab", { name: "Tuner" }));

    expect(screen.getByText(/In use by APRS logging/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Release & listen/ }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(false));
  });
});
