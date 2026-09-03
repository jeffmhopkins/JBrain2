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
import { ApiError, api } from "../api/client";
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
    window_packets: { "1d": 12, "3d": 12, "1w": 12 },
    has_older: false,
    older: null,
    truncated: false,
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

/** One heard packet, decoded, as the API now sends it. */
function packet(over: Record<string, unknown> = {}) {
  return {
    id: "pkt-1",
    heard_at: new Date().toISOString(),
    kind: "Position",
    bucket: "Position",
    gated: true,
    direct: false,
    text: "!2835.06ND08048.98W&RNG0001 2m Voice",
    summary: "Car — 52 knots (60 mph) heading 242° (WSW)",
    fields: [
      ["Position", "28.5108, -81.3963"],
      ["Symbol", "Car"],
    ],
    comment: "On D-Star K1XC",
    symbol: "/>",
    warnings: [],
    relay: "N4TDX",
    audio_level: null,
    lat: 28.6212,
    lon: -80.8237,
    frame: { source: "N4TDX", destination: "APDG02", path: ["WIDE1-1"] },
    ...over,
  };
}

/** One station's traffic, as the detail view reads it. */
function station(call: string, over: Record<string, unknown> = {}) {
  return {
    call,
    packets_total: 4,
    last_heard_at: new Date().toISOString(),
    gated: call === "N1MPR-C",
    relay: call === "N1MPR-C" ? "N4TDX" : null,
    window: "1d",
    window_packets: { "1d": 4, "3d": 4, "1w": 4 },
    has_older: false,
    older: 0,
    kind_packets: { Position: 4 },
    packets: [packet({ gated: call === "N1MPR-C", direct: call !== "N1MPR-C" })],
    ...over,
  };
}

/** Which station's detail is on screen, read from the header the roster does not have. */
function openStation(container: HTMLElement): string | null {
  return container.querySelector(".aprs-st-head .aprs-st-call")?.textContent ?? null;
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
      window_packets: { "1d": 4, "3d": 4, "1w": 4 },
      has_older: false,
      older: 0,
      kind_packets: { Position: 4 },
      packets: [packet()],
    } as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));

    // The frame the station composed. Rendering the stored `info` would print the
    // third-party transport in front of every relayed line instead of the content.
    expect(await screen.findByText(/52 knots .* heading 242/)).toBeInTheDocument();
    expect(detail).toHaveBeenCalledWith("N1MPR-C", "1d", []);
    // Provenance per packet, which is also where the untrusted rule meets the eye.
    expect(screen.getByText("gated")).toBeInTheDocument();
    // The row is a sentence; the bytes are one tap below, not on the row.
    expect(screen.queryByText(/2835\.06ND/)).toBeNull();
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
    await waitFor(() => expect(asked).toHaveBeenCalledWith("1d", ["Weather"], null));
  });

  it("puts the meaning on the row and the bytes one tap below", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(station("N1MPR-C") as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));
    const row = await screen.findByRole("button", { expanded: false, name: /52 knots/ });

    // Collapsed: a sentence, and the station's own words. Not the frame.
    expect(row).toHaveTextContent("On D-Star K1XC");
    expect(row).not.toHaveTextContent("2835.06ND");

    fireEvent.click(row);

    // Expanded: the field breakdown AND the frame, so every sentence can be checked.
    expect(await screen.findByText("28.5108, -81.3963")).toBeInTheDocument();
    expect(screen.getByText(/The frame as heard/)).toBeInTheDocument();
    expect(screen.getByText(/2835\.06ND/)).toBeInTheDocument();
  });

  it("does not restate the icon as text", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // A position whose whole reading IS the symbol's name. The glyph already says it,
    // and so does that glyph's accessible label — a third copy in words spends a line
    // on a fact the row has already made twice.
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("KE8XYZ-9", {
        packets: [packet({ summary: "Car", comment: "", symbol: "/>" })],
      }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    const row = await screen.findByRole("button", { expanded: false, name: /Position/ });

    expect(row.querySelector(".aprs-said")).toBeNull();
    // But it is still reachable: the icon carries it for a screen reader, and the
    // detail carries it for a reader who taps.
    expect(screen.getByRole("img", { name: "Car" })).toBeInTheDocument();
  });

  it("does not speak in the station's voice when the words are the station's", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // A status report IS its own summary. Repeating it in the app's font would present
    // a stranger's sentence as ours — the one thing the two-voice rule exists to stop.
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("K4JTT-D", {
        packets: [
          packet({
            kind: "Status",
            summary: "",
            comment: "Powered by WPSD (https://wpsd.radio)",
            symbol: "",
            fields: [],
          }),
        ],
      }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    const quoted = await screen.findByText(/Powered by WPSD/);

    expect(quoted).toHaveClass("aprs-theirs");
    // A URL from an anonymous transmitter is never linkified.
    expect(quoted.querySelector("a")).toBeNull();
  });

  it("shows a station's own symbol differently from our guess about the packet", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("N1KSC-1", {
        packets: [
          packet({ id: "a", symbol: "/>", fields: [["Symbol", "Car"]] }),
          packet({ id: "b", kind: "Telemetry", symbol: "", fields: [], summary: "14.25 Volt" }),
        ],
      }) as never,
    );

    const { container } = render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    await screen.findByText(/14.25 Volt/);

    // An APRS symbol is a claim the STATION made; a kind glyph is a claim WE made.
    expect(container.querySelectorAll(".aprs-sym-own")).toHaveLength(1);
    expect(container.querySelectorAll(".aprs-sym-kind")).toHaveLength(1);
  });

  it("surfaces what could not be read instead of swallowing it", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("K4KSC-12", {
        packets: [
          packet({
            kind: "Telemetry",
            symbol: "",
            summary: "Channel 1 053",
            fields: [["Channel 1", "053"]],
            comment: "",
            warnings: ["This station has not published what its channels measure."],
          }),
        ],
      }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    fireEvent.click(await screen.findByRole("button", { expanded: false, name: /Telemetry/ }));

    // The alternative is inventing units for an undeclared channel, which puts a
    // confident wrong reading on screen with nothing to contradict it.
    expect(await screen.findByText(/has not published/)).toBeInTheDocument();
  });

  it("does not repeat the callsign the header already carries", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(station("N1MPR-C") as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));
    const row = await screen.findByRole("button", { expanded: false, name: /52 knots/ });

    // Inside a station every packet is that station's, so a callsign in every title is
    // one fact repeated down the whole list, crowding out the only part that varies.
    expect(row.querySelector(".aprs-packet-title")?.textContent).toBe("Position");
    expect(row.querySelector(".aprs-call")).toBeNull();
    // How it REACHED us is still worth saying, and that is a different callsign.
    expect(row).toHaveTextContent("relayed by N4TDX");
  });

  it("keeps a row expanded when a poll prepends a newer packet", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // Distinct `text`, because that is what makes two frames two facts — a fixture
    // differing only in its decoded summary describes one frame, and folds.
    const older = packet({
      id: "older",
      summary: "Car — 52 knots (60 mph) heading 242° (WSW)",
      text: "!2835.06ND08048.98W>242/052",
    });
    const newer = packet({
      id: "newer",
      summary: "Truck — 12 knots (14 mph) heading 90° (E)",
      text: "!2835.06ND08048.98W>090/012",
    });
    vi.spyOn(api, "getAprsStation")
      .mockResolvedValueOnce(station("N1MPR-C", { packets: [older] }) as never)
      .mockResolvedValue(station("N1MPR-C", { packets: [newer, older] }) as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));
    fireEvent.click(await screen.findByRole("button", { expanded: false, name: /52 knots/ }));
    expect(await screen.findByText("28.5108, -81.3963")).toBeInTheDocument();

    // Focus the row, the way a keyboard or screen-reader user is holding it.
    const open = screen.getByRole("button", { expanded: true, name: /52 knots/ });
    open.focus();

    // Now a poll lands and a newer frame goes on top. Keyed on the ARRAY INDEX, the
    // focused DOM node is reused for a DIFFERENT packet — focus silently moves to
    // somebody else's frame, and the reader is never told.
    fireEvent.click(screen.getByRole("button", { name: /3 days/ }));

    await waitFor(() => expect(screen.getByText(/12 knots/)).toBeInTheDocument());
    expect(screen.getByRole("button", { expanded: true, name: /52 knots/ })).toBeInTheDocument();
    expect(document.activeElement).toHaveTextContent("52 knots");
    expect(document.activeElement).not.toHaveTextContent("12 knots");
  });

  it("says how strong a transmission was, where that was measured", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("N1MPR-C", {
        packets: [packet({ id: "loud", audio_level: 72 })],
      }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));

    // Measured by the box, unlike every other fact on the row — so it is tinted rather
    // than left neutral, and it carries the raw number for anyone who wants it.
    const signal = await screen.findByText("strong");
    expect(signal).toHaveClass("aprs-sig");
    expect(signal).toHaveAttribute("title", "Audio level 72 of 100");
  });

  it("says nothing about signal for a frame nobody measured", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // Every row logged before the level was captured looks like this, for ever — the
    // reading only ever existed at decode time. Null is not zero, and "weak" here would
    // invent the one fact on the row that is not self-declared by a stranger.
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("N1MPR-C", { packets: [packet({ audio_level: null })] }) as never,
    );

    const { container } = render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));
    await screen.findByRole("button", { expanded: false, name: /52 knots/ });

    expect(container.querySelector(".aprs-sig")).toBeNull();
  });

  it("says where a station is, once stripping the icon's name leaves nothing", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // A plain position: its whole reading IS the symbol name, so the icon rule empties
    // it. Without a fallback the row said the bare word "Position" — which is what the
    // owner's own screen showed.
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("KC3EFJ", {
        packets: [
          packet({
            kind: "Position",
            summary: "Phone",
            symbol: "/$",
            comment: "",
            fields: [
              ["Position", "28.6212, -80.8237"],
              ["Symbol", "Phone"],
              ["Altitude", "-85 ft (-26 m)"],
              ["Grid square", "EL98oo"],
            ],
          }),
        ],
      }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    const row = await screen.findByRole("button", { expanded: false, name: /Position/ });

    // With no location allowed it falls through to the grid square — which needs
    // nothing but the frame — plus the altitude the owner asked to see.
    expect(row).toHaveTextContent("EL98oo");
    expect(row).toHaveTextContent("-85 ft");
    // And never the icon's own name AS TEXT. It still belongs in the glyph's accessible
    // title, which is how a screen-reader user gets the one fact a sighted reader gets
    // from the picture — so this checks the written lines, not the whole subtree.
    expect(row.querySelector(".aprs-said")?.textContent).toBe("EL98oo · -85 ft");
    expect(row.querySelector(".aprs-packet-title")?.textContent).toBe("Position");
    expect(row.querySelector("svg title")?.textContent).toBe("Phone");
  });

  it("measures from the phone when the reader allows it", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("KC3EFJ", {
        packets: [
          packet({
            summary: "Phone",
            symbol: "/$",
            comment: "",
            fields: [
              ["Symbol", "Phone"],
              ["Grid square", "EL98oo"],
            ],
          }),
        ],
      }) as never,
    );
    vi.stubGlobal("navigator", {
      geolocation: {
        getCurrentPosition: (ok: PositionCallback) =>
          ok({
            coords: { latitude: 28.61, longitude: -80.81, accuracy: 15 },
          } as GeolocationPosition),
      },
    });

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));

    // Range beats the grid when the phone knows where it is: it answers "how far is
    // that from me", which is also this box's reception range.
    expect(await screen.findByText(/1\.1 mi NW/)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("folds a run of identical beacons into one row that says how many", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // 25 of the owner's rows were one D-STAR object re-announced every twenty minutes.
    const beacon = (id: string) =>
      packet({
        id,
        kind: "Object",
        summary: "N1MPR C",
        text: ";N1MPR C  *111111z2835.06ND08048.98Wa",
      });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("N1MPR-C", { packets: [beacon("a"), beacon("b"), beacon("c")] }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));

    expect(await screen.findByText("heard 3×")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /N1MPR C/ })).toHaveLength(1);
  });

  it("keeps two readings apart even when they arrive back to back", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // Only byte-identical payloads fold. Two weather readings a degree apart are two
    // facts, and collapsing them would hide the change that makes them worth having.
    vi.spyOn(api, "getAprsStation").mockResolvedValue(
      station("WA4IKQ", {
        packets: [
          packet({ id: "w1", kind: "Weather", summary: "82 °F", text: "...t082..." }),
          packet({ id: "w2", kind: "Weather", summary: "81 °F", text: "...t081..." }),
        ],
      }) as never,
    );

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    await screen.findByText("82 °F");

    expect(screen.getByText("81 °F")).toBeInTheDocument();
    expect(screen.queryByText(/heard \d+×/)).toBeNull();
  });

  it("shows the frame without a second tap once the card is open", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockResolvedValue(station("N1MPR-C") as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));
    fireEvent.click(await screen.findByRole("button", { expanded: false, name: /52 knots/ }));

    // A reader who expanded the card has already asked to see more; a second
    // disclosure was a tap that bought nothing.
    expect(await screen.findByText(/2835\.06ND/)).toBeVisible();
  });

  it("keeps a way out when a station fails to load", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    vi.spyOn(api, "getAprsStation").mockRejectedValue(new ApiError(404, "nothing heard"));

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByText("N1MPR-C"));

    // This screen has made the opposite mistake once already: the error render sat AFTER
    // the loading return, so a failed load spun on "Reading…" for ever. One level down it
    // would be worse — the only "All stations" button lived inside the detail that never
    // arrived, so a 404 stranded the owner with no exit but leaving the tab.
    expect(await screen.findByText(/nothing heard/)).toBeInTheDocument();
    const out = screen.getByRole("button", { name: /All stations/ });
    fireEvent.click(out);
    expect(await screen.findByText("KE8XYZ-9")).toBeInTheDocument();
  });

  it("does not throw away a working roster when a later poll fails", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const asked = vi
      .spyOn(api, "getAprsStations")
      .mockResolvedValueOnce(roster() as never)
      .mockRejectedValue(new ApiError(500, "the log is unreadable"));

    render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("KE8XYZ-9");
    // Changing the range re-asks, and this time the server fails.
    fireEvent.click(screen.getByRole("button", { name: /3 days/ }));

    // The error is SAID — a list that silently freezes on stale rows under a healthy
    // header is the same lie as a dead receiver reading as a quiet channel — and the
    // stations already on screen stay, because throwing them away helps nobody.
    expect(await screen.findByText(/the log is unreadable/)).toBeInTheDocument();
    expect(screen.getByText("KE8XYZ-9")).toBeInTheDocument();
    expect(asked).toHaveBeenCalledWith("3d", [], null);
  });

  it("ignores a slow response that a newer request has replaced", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    let releaseSlow: (value: unknown) => void = () => {};
    const slow = new Promise((resolve) => {
      releaseSlow = resolve;
    });
    vi.spyOn(api, "getAprsStation")
      .mockImplementationOnce(async () => (await slow) as never)
      .mockResolvedValue(station("KE8XYZ-9") as never);

    const { container } = render(<RadioScreen onClose={() => {}} />);
    // Open the slow station, go back, open the fast one.
    fireEvent.click(await screen.findByText("N1MPR-C"));
    fireEvent.click(screen.getByRole("button", { name: /All stations/ }));
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    await waitFor(() => expect(openStation(container)).toBe("KE8XYZ-9"));
    // Now the first request finally lands.
    releaseSlow(station("N1MPR-C"));

    // Without a sequence guard it overwrites the screen: N1MPR-C's packets and counts
    // under a header the owner never asked for, while the chips and range tabs go on
    // issuing requests for KE8XYZ-9.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(openStation(container)).toBe("KE8XYZ-9");
  });

  it("keeps a carried-in type filter visible so it can be cleared", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    // KE8XYZ-9 sends only positions, so a Weather filter carried in matches nothing —
    // the server answers with an empty list and no Weather in `kind_packets`.
    const detail = vi
      .spyOn(api, "getAprsStation")
      .mockResolvedValue(station("KE8XYZ-9", { packets: [] }) as never);

    render(<RadioScreen onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: /Weather/, pressed: false }));
    fireEvent.click(await screen.findByText("KE8XYZ-9"));
    await waitFor(() => expect(detail).toHaveBeenCalledWith("KE8XYZ-9", "1d", ["Weather"]));

    // The chip row is built from the kinds the STATION sends, so a zero-count selection
    // used to vanish — leaving an empty list, a message saying "clear the type filter",
    // and no filter on screen to clear.
    fireEvent.click(await screen.findByRole("button", { name: /Weather/, pressed: true }));

    await waitFor(() => expect(detail).toHaveBeenCalledWith("KE8XYZ-9", "1d", []));
  });

  it("offers the time range, and asks the server for it", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    const asked = stations();

    render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("KE8XYZ-9");
    fireEvent.click(screen.getByRole("button", { name: /1 week/ }));

    await waitFor(() => expect(asked).toHaveBeenCalledWith("1w", [], null));
  });

  it("says nothing about how much is older until it is worth the read", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    stations({ has_older: true, older: null });

    render(<RadioScreen onClose={() => {}} />);

    // Counting everything older than a week means reading the whole archive on every
    // poll. The tab still exists — there IS older traffic — it just does not claim a
    // number it did not count.
    const older = await screen.findByRole("button", { name: "Older" });
    expect(older.querySelector(".seg-count")).toBeNull();
    expect(screen.getByRole("button", { name: /1 day, 12 packets/ })).toBeInTheDocument();
  });

  it("says when the list was capped rather than printing a confident total", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });
    stations({ truncated: true, stations_total: 412 });

    render(<RadioScreen onClose={() => {}} />);

    expect(await screen.findByText(/of 412, newest first/)).toBeInTheDocument();
  });

  it("does not cry unsorted when everything is sorted", async () => {
    vi.spyOn(api, "getAprsPackets").mockResolvedValue(log() as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({ available: true, listening: null });

    render(<RadioScreen onClose={() => {}} />);
    await screen.findByText("KE8XYZ-9");

    expect(screen.queryByText(/not sorted yet/)).toBeNull();
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
    const tabbar = container.querySelector('[role="tablist"]');
    expect(tabbar).not.toBeNull();
    expect(tabbar?.classList.contains("seg-tabs")).toBe(true);
    expect(tabbar?.querySelectorAll(".seg-tab")).toHaveLength(3);
    expect(container.querySelector(".radio-tabs")).toBeNull();
    // And the roster's range control REUSES it rather than cloning it — the near-identical
    // copy under a different class name is precisely what this test is written against,
    // and it would have slipped past a count of the whole document.
    expect(container.querySelectorAll(".seg-tabs")).toHaveLength(2);
    expect(container.querySelector(".aprs-windows")).toBeNull();
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
