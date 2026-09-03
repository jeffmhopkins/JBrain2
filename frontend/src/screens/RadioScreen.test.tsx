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

/** A roster as the server sends it. The two stations are the measured case in
 * miniature: one heard directly, one that only exists because a relayed frame was
 * unwrapped — its AX.25 source was the IGate, not N1MPR-C. */
function roster(over: Record<string, unknown> = {}) {
  return {
    window: "1d",
    window_packets: { "1d": 12, "3d": 12, "1w": 12, old: 0 },
    unclassified: 0,
    kind_stations: { Position: 2, Weather: 1 },
    stations_total: 2,
    stations: [
      {
        call: "KE8XYZ-9",
        packets: 8,
        last_heard_at: new Date().toISOString(),
        kinds: ["Position"],
        gated: false,
        relay: null,
      },
      {
        call: "N1MPR-C",
        packets: 4,
        last_heard_at: new Date(Date.now() - 600_000).toISOString(),
        kinds: ["Position", "Weather"],
        gated: true,
        relay: "N4TDX",
      },
    ],
    ...over,
  };
}

function stations(over: Record<string, unknown> = {}) {
  return vi.spyOn(api, "getAprsStations").mockResolvedValue(roster(over) as never);
}

beforeEach(() => {
  noCommands();
  stations();
  // The callsign lives in app Settings, not on this screen. Most tests do not care
  // which station is the owner's; the ones that do override this.
  vi.spyOn(api, "getSettings").mockResolvedValue({ owner_callsign: null } as never);
});

afterEach(() => {
  resetSdrSession();
  vi.restoreAllMocks();
});

describe("the APRS tab", () => {
  it("lists who was heard, and how each one reached the box", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
    });

    render(<RadioScreen onClose={() => {}} />);

    // Stations, not frames. On the owner's own capture a flat feed showed 6 callsigns
    // for 16 transmitting stations, because three quarters of the channel was one
    // IGate relaying internet traffic under its own name.
    expect(await screen.findByText("KE8XYZ-9")).toBeInTheDocument();
    expect(screen.getByText("N1MPR-C")).toBeInTheDocument();
    // And the difference between the two is stated, not implied: one was on the air,
    // the other never was.
    expect(screen.getByText(/heard on RF/)).toBeInTheDocument();
    expect(screen.getByText(/gated via N4TDX/)).toBeInTheDocument();
  });

  it("says the roster is incomplete rather than quietly listing fewer stations", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    stations({ unclassified: 41 });

    render(<RadioScreen onClose={() => {}} />);

    // Same rule as the health line above it: a list still filling in must not look
    // like a channel with fewer stations on it.
    expect(await screen.findByText(/41 packets not sorted yet/)).toBeInTheDocument();
  });

  it("opens a station and shows the payload, not the wrapper it arrived in", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const detail = vi.spyOn(api, "getAprsStation").mockResolvedValue({
      call: "N1MPR-C",
      packets_total: 4,
      last_heard_at: new Date().toISOString(),
      gated: true,
      relay: "N4TDX",
      window: "1d",
      window_packets: { "1d": 4, "3d": 4, "1w": 4, old: 0 },
      kind_packets: { Position: 4 },
      packets: [
        {
          heard_at: new Date().toISOString(),
          kind: "Position",
          gated: true,
          direct: false,
          text: "!2835.06ND08048.98W&RNG0001 2m Voice",
        },
      ],
    } as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));

    // The frame the station composed. Rendering the stored `info` would print the
    // third-party transport in front of every relayed line instead of the content.
    expect(await screen.findByText(/2m Voice/)).toBeInTheDocument();
    expect(detail).toHaveBeenCalledWith("N1MPR-C", "1d", []);
    // Provenance per packet, which is also where the untrusted rule meets the eye.
    expect(screen.getByText("gated")).toBeInTheDocument();
  });

  it("narrows the ROSTER by type, not the packets", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const asked = stations();

    render(<RadioScreen onClose={() => {}} />);
    // `pressed` picks the chip out: a station row whose kinds include Weather matches
    // the name too, and it is not a toggle.
    fireEvent.click(await screen.findByRole("button", { name: /Weather/, pressed: false }));

    // "Show me who is putting out weather" is a question about stations, and the
    // server answers it — a client that downloaded the log to narrow it here would
    // move a year of a 1.2M-row channel to render two lines.
    await waitFor(() => expect(asked).toHaveBeenCalledWith("1d", ["Weather"]));
  });

  it("pins the owner's own stations, however they reached the box", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // The bare call in Settings, and the station is an SSID of it: the truck and the
    // handheld are one operator.
    vi.spyOn(api, "getSettings").mockResolvedValue({ owner_callsign: "N1MPR" } as never);

    render(<RadioScreen onClose={() => {}} />);

    // The trap this removes: his own mail arrives WRAPPED, because an IGate relays a
    // message to RF only once the addressee has been heard nearby. Filed under the
    // relay it reads as somebody else's noise.
    const rows = await screen.findAllByRole("button", { name: /pkt/ });
    expect(rows[0]).toHaveTextContent("N1MPR-C");
    expect(rows[0]).toHaveClass("mine");
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
          once: false,
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
          once: false,
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
          once: false,
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
    await screen.findByText("KE8XYZ-9");
    fireEvent.click(screen.getByRole("tab", { name: "Tuner" }));

    expect(screen.getByText(/In use by APRS logging/)).toBeInTheDocument();
    // The mock states the elapsed hold; without it "release this" is a decision made
    // with no idea whether the session started a minute or a day ago.
    expect(screen.getByText(/Held for 1h 12m/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Release & listen/ }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(false));
  });
});

describe("the screen's shell", () => {
  it("covers the launcher rather than letting it show through", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });

    const { container } = render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("KE8XYZ-9");

    // The launcher deliberately stays open BENEATH a card — dismissing the card
    // reveals it again — so a card that is only a padded section shows the tiles
    // through it. `subscreen` is the shared fixed-inset layer every sibling uses.
    expect(container.querySelector(".subscreen")).not.toBeNull();
  });

  it("uses the house segmented control, not a bespoke tab bar", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });

    const { container } = render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("KE8XYZ-9");

    // The same control the session list uses for Today / Older / Archived. This screen
    // had invented an underline tab bar — a second answer to a settled question.
    expect(container.querySelector(".seg-tabs")).not.toBeNull();
    expect(container.querySelectorAll(".seg-tab")).toHaveLength(3);
    expect(container.querySelector(".radio-tabs")).toBeNull();
  });
});

describe("the Tuner tab", () => {
  it("mounts the real transport, not a description of it", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(
      log({ logging: false, packets: [] }) as never,
    );
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("listen") as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Tuner" }));

    // The one screen called "Radio" used to be the one place you could not work the
    // radio: it printed frequency and mode and pointed at the composer's icon. These
    // are the omnibox sheet's own controls, mounted here.
    expect(screen.getByRole("button", { name: /Tap to enter a frequency/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Release" })).toBeInTheDocument();
  });

  it("still hands the radio back when APRS is holding it", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue(lease("aprs") as never);
    const set = vi
      .spyOn(api, "setAprsLogging")
      .mockResolvedValue({ logging: false, changed: true });

    render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("KE8XYZ-9");
    fireEvent.click(screen.getByRole("tab", { name: "Tuner" }));

    // A logging session is not audible, so the transport would be meaningless here —
    // the handoff is the only useful control.
    expect(screen.queryByRole("button", { name: "Release" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Release & listen/ }));
    await waitFor(() => expect(set).toHaveBeenCalledWith(false));
  });
});
