// The Radio launcher's APRS tab.
//
// What is pinned here is every way this screen could tell the owner something false
// about the radio, because an independent review found four of them: a dead receiver
// reading as a quiet channel or as a switched-off one, a failed first load spinning on
// "Reading the log…" for ever, and a switch offered where it could only fail. Plus the
// two rules the surface exists to keep — heard packets rendered as somebody else's
// transmission, and the one-dongle handoff offered from both sides.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
  return {
    logging: true,
    reachable: true,
    frequency_hz: 144_390_000,
    packets: [PACKET],
    ...over,
  };
}

/** A held lease, as the 1 s session poll reports it. Who holds the tuner is read from
 * HERE and nowhere else — the log poll carries the same fact five seconds later, and
 * mixing the two let the tabs disagree with each other. */
function lease(purpose: string, elapsed_s = 4320) {
  return {
    available: true,
    listening: {
      session_id: "s1",
      frequency_hz: 144_390_000,
      mode: "fm",
      gain: null,
      purpose,
      started_at: 0,
      elapsed_s,
      peak: 0,
      listeners: 0,
    },
  };
}

function noCommands() {
  return vi.spyOn(api, "getAprsCommands").mockResolvedValue({ commands: [], attempts: [] });
}

beforeEach(() => {
  noCommands();
});

afterEach(() => {
  resetSdrSession();
  vi.restoreAllMocks();
});

describe("the APRS tab", () => {
  it("shows what was heard", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText("GATE 7K2M9")).toBeInTheDocument();
    expect(screen.getByText("KE8XYZ-9")).toBeInTheDocument();
  });

  it("badges a packet as heard rather than presenting it as ours", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });

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
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/nothing for \d+ min/)).toBeInTheDocument();
  });

  it("offers the switch when logging is off, and says what it costs", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });
    const set = vi.spyOn(api, "setAprsLogging").mockResolvedValue({ logging: true, changed: true });

    render(<RadioScreen onClose={() => {}} />);

    const button = await screen.findByRole("button", {
      name: "Enable APRS logging",
    });
    // The cost is named where the switch is, not in a hint read earlier.
    expect(screen.getByText(/Reserves the tuner until released/)).toBeInTheDocument();
    fireEvent.click(button);
    await waitFor(() => expect(set).toHaveBeenCalledWith(true));
  });

  it("surfaces which job holds the radio when the switch is refused", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });
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

  it("says the receiver is unreachable rather than switched off", async () => {
    // A dead receiver reading as a switched-off one is the confusion the health line
    // exists to prevent, and `logging: false` alone cannot tell them apart.
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, reachable: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/isn't reachable/)).toBeInTheDocument();
  });

  it("shows the error when the FIRST load fails, instead of loading for ever", async () => {
    const { ApiError } = await import("../api/client");
    vi.spyOn(api, "getAprsPackets").mockRejectedValue(new ApiError(503, "No SDR on this box."));
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: false,
      listening: null,
    });

    render(<RadioScreen onClose={() => {}} />);

    // The error render used to sit AFTER the loading return, so this tab spun on
    // "Reading the log…" for ever with the message swallowed — the default experience
    // on a box with no radio, because the launcher offers the Radio tile regardless.
    expect(await screen.findByText("No SDR on this box.")).toBeInTheDocument();
    expect(screen.queryByText("Reading the log…")).not.toBeInTheDocument();
  });

  it("names the listening session BEFORE offering a switch that can only fail", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("listen") as never);
    const set = vi.spyOn(api, "setAprsLogging").mockResolvedValue({ logging: true, changed: true });

    render(<RadioScreen onClose={() => {}} />);

    // One dongle, one job (docs/mocks/aprs/c-single-dongle.html shape A). Offering
    // "Enable APRS logging" here produced a raw lowercase 409 string AFTER the tap.
    expect(await screen.findByText(/The radio is listening/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable APRS logging" })).not.toBeInTheDocument();

    // Exactly one CTA — round 3's own review killed a duplicate here.
    fireEvent.click(screen.getByRole("button", { name: /Release & log APRS/ }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(true));
  });

  it("shows how long logging has held the tuner", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("aprs") as never);

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/Holding the tuner for 1h 12m/)).toBeInTheDocument();
  });

  it("keeps the decode rate on screen", async () => {
    // The rate is half of "is it alive" and was deletable with every test still green.
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ packets: [PACKET, { ...PACKET, info: "second" }] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("aprs") as never);

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/pkt\/hr/)).toBeInTheDocument();
  });

  it("says a command is armed but deaf when nothing is receiving", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsCommands").mockResolvedValue({
      commands: [
        {
          id: "t1",
          name: "Open the gate",
          enabled: true,
          word: "GATE",
          callsign: "KE8XYZ-9",
          days: [1, 2, 3, 4, 5],
          from: "06:00",
          until: "09:00",
          locked: false,
          last_at: null,
        },
      ],
      attempts: [],
    });

    render(<RadioScreen onClose={() => {}} />);

    // Arming a task and enabling the receiver are two switches on purpose, so a task
    // that says "armed" while nothing is receiving is the same lie a signal meter on a
    // dead channel tells. Round 3's own review called this the thing most likely to be
    // missed in the build.
    expect(await screen.findByText(/Armed, but nothing is receiving/)).toBeInTheDocument();
    expect(screen.getByText("armed — not listening")).toBeInTheDocument();
  });

  it("does not cry deaf while the receiver is up", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("aprs") as never);
    vi.spyOn(api, "getAprsCommands").mockResolvedValue({
      commands: [
        {
          id: "t1",
          name: "Open the gate",
          enabled: true,
          word: "GATE",
          callsign: null,
          days: [1, 2, 3, 4, 5],
          from: "06:00",
          until: "09:00",
          locked: false,
          last_at: null,
        },
      ],
      attempts: [],
    });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/armed weekdays 06:00–09:00/)).toBeInTheDocument();
    expect(screen.queryByText(/Armed, but nothing is receiving/)).not.toBeInTheDocument();
  });

  it("shows refused attempts, which are the ones worth keeping", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("aprs") as never);
    vi.spyOn(api, "getAprsCommands").mockResolvedValue({
      commands: [
        {
          id: "t1",
          name: "Open the gate",
          enabled: true,
          word: "GATE",
          callsign: null,
          days: [],
          from: null,
          until: null,
          locked: false,
          last_at: null,
        },
      ],
      attempts: [
        {
          heard_at: new Date().toISOString(),
          source: "N0BODY-1",
          word: "GATE",
          accepted: false,
          reason: "code did not verify",
        },
      ],
    });

    render(<RadioScreen onClose={() => {}} />);

    // "Every attempt is visible" is a P4 exit criterion, and a push does not satisfy
    // it: pushes are ephemeral and are exactly what an attacker hopes goes unread.
    expect(await screen.findByText(/code did not verify/)).toBeInTheDocument();
    expect(screen.getByText("N0BODY-1")).toBeInTheDocument();
  });
});

describe("the Tuner tab", () => {
  it("says when APRS is holding the radio, and offers it back", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("aprs") as never);
    const set = vi
      .spyOn(api, "setAprsLogging")
      .mockResolvedValue({ logging: false, changed: true });

    render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("GATE 7K2M9");
    fireEvent.click(screen.getByRole("tab", { name: "Tuner" }));

    expect(screen.getByText(/In use by APRS logging/)).toBeInTheDocument();
    // The mock states the elapsed hold; without it "release this" is a decision made
    // with no idea whether the session started a minute or a day ago.
    expect(screen.getByText(/Held for 1h 12m/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Release & listen/ }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(false));
  });
});
