// The omnibox radio sheet against its binding spec
// (docs/mocks/omnibox-radios/d-radio-then-task.html, shape D).
//
// The properties worth pinning are the ones the old sheet got wrong: that it opens on a
// radio doing something OTHER than listening, that the tapped radio is the one shown,
// and that a second radio is reachable at all. The job surfaces themselves are the
// Radios tab's — the same `RadioJob` — and are tested there rather than twice.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { resetBands } from "../sdrBands";
import { resetSdrSession } from "../sdrSession";
import { resetSdrSpectrum } from "../sdrSpectrum";
import { SdrRadiosSheet, tabbable } from "./SdrRadiosSheet";

/** The store's stream, stubbed so the poll has something to open. */
class FakeSource {
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  close(): void {}
}

const WHIP = "09022796";
const WIRE = "77192819";

function radio(serial: string, over: Record<string, unknown> = {}) {
  return {
    serial,
    name: "",
    description: "",
    role: "general",
    attached: true,
    gain: "",
    upconverter_hz: 0,
    ...over,
  };
}

function session(over: Record<string, unknown> = {}) {
  return {
    session_id: "s1",
    frequency_hz: 144_390_000,
    mode: "fm",
    gain: null,
    started_at: 0,
    elapsed_s: 30,
    audio_peak: 0,
    listeners: 0,
    ...over,
  };
}

function box(radios: ReturnType<typeof radio>[], sessions: ReturnType<typeof session>[] = []) {
  vi.spyOn(api, "getSdrRadios").mockResolvedValue({
    radios,
    conflicts: {},
    scan_ok: true,
  } as never);
  vi.spyOn(api, "getSdrStatus").mockResolvedValue({
    available: true,
    listening: sessions[0] ?? null,
    sessions,
  } as never);
}

function show(openOn: string | null, onOpenAprs = () => {}) {
  return render(<SdrRadiosSheet openOn={openOn} onOpenAprs={onOpenAprs} onClose={() => {}} />);
}

beforeEach(() => {
  vi.stubGlobal("EventSource", FakeSource);
});

afterEach(() => {
  resetBands();
  resetSdrSession();
  resetSdrSpectrum();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the omnibox radio sheet", () => {
  it("opens on a radio that is NOT listening", async () => {
    // The defect the whole shape exists to fix: the icon opened a tuner built from
    // `listening`, so a radio decoding APRS got a transport drawn for something
    // producing no audio — and with APRS the only session, nothing at all.
    box([radio(WIRE, { name: "Long wire" })], [session({ purpose: "aprs", serial: WIRE })]);

    show(WIRE);

    expect(await screen.findByRole("button", { name: /Open the APRS log/ })).toBeInTheDocument();
    // The transport belongs to the `listen` job and must not be drawn for this one.
    expect(screen.queryByRole("button", { name: "Tune up" })).not.toBeInTheDocument();
  });

  it("shows the radio the icon was tapped on, not the first one", async () => {
    box(
      [radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })],
      [
        session({ session_id: "s-tuner", purpose: "listen", serial: WHIP }),
        session({ session_id: "s-aprs", purpose: "aprs", serial: WIRE }),
      ],
    );

    show(WIRE);

    const tab = await screen.findByRole("button", { name: /Long wire/ });
    expect(tab).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Desk whip/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByRole("button", { name: /Open the APRS log/ })).toBeInTheDocument();
  });

  it("switches to the other radio's job when its tab is tapped", async () => {
    // The point of the sheet: a second radio is reachable without leaving the composer.
    box(
      [radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })],
      [
        session({ session_id: "s-tuner", purpose: "listen", serial: WHIP }),
        session({ session_id: "s-aprs", purpose: "aprs", serial: WIRE }),
      ],
    );

    show(WIRE);

    fireEvent.click(await screen.findByRole("button", { name: /Desk whip/ }));

    expect(await screen.findByRole("button", { name: "Tune up" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open the APRS log/ })).not.toBeInTheDocument();
  });

  it("names each radio's job on its tab", async () => {
    box(
      [radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })],
      [
        session({ session_id: "s-tuner", purpose: "listen", serial: WHIP }),
        session({ session_id: "s-spec", purpose: "spectrum", serial: WIRE }),
      ],
    );

    show(WHIP);

    expect(await screen.findByRole("button", { name: /Desk whip Listen/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Long wire Spectrum/ })).toBeInTheDocument();
  });

  it("draws no tab row for a single radio", async () => {
    // With one radio the row is nothing but chrome: it offers a choice that does not
    // exist, on the surface with the least room on the screen.
    box([radio(WHIP, { name: "Desk whip" })], [session({ purpose: "listen", serial: WHIP })]);

    show(WHIP);

    expect(await screen.findByRole("button", { name: "Tune up" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Desk whip/ })).not.toBeInTheDocument();
  });

  it("falls back to a real radio when the one it opened on is gone", async () => {
    // The omnibox names the radio its icon was reflecting; by the time the roster
    // arrives that dongle may have been unplugged. Showing nothing would be a sheet
    // that opens empty over a box plainly holding a radio.
    box([radio(WHIP, { name: "Desk whip" })], [session({ purpose: "listen", serial: WHIP })]);

    show("a-serial-that-left");

    expect(await screen.findByRole("button", { name: "Tune up" })).toBeInTheDocument();
  });

  it("says so when the roster cannot be read", async () => {
    vi.spyOn(api, "getSdrRadios").mockRejectedValue(new Error("nope"));
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
      sessions: [],
    } as never);

    show(null);

    expect(await screen.findByRole("alert")).toHaveTextContent(/Couldn't read the radios/);
  });

  it("leaves for the APRS log rather than fetching one behind the composer", async () => {
    const leave = vi.fn();
    box([radio(WIRE, { name: "Long wire" })], [session({ purpose: "aprs", serial: WIRE })]);

    show(WIRE, leave);

    fireEvent.click(await screen.findByRole("button", { name: /Open the APRS log/ }));

    expect(leave).toHaveBeenCalled();
  });

  it("keeps Escape inside the frequency field instead of closing the sheet", async () => {
    // The sheet dismisses on Escape, and the tuner's frequency field is inside it:
    // abandoning a mistyped digit must not also close the radio the owner is driving.
    const onClose = vi.fn();
    box([radio(WHIP, { name: "Desk whip" })], [session({ purpose: "listen", serial: WHIP })]);

    render(<SdrRadiosSheet openOn={WHIP} onOpenAprs={() => {}} onClose={onClose} />);

    fireEvent.click(await screen.findByRole("button", { name: /Tap to enter a frequency/ }));
    const field = screen.getByRole("textbox", { name: "Frequency in MHz" });
    fireEvent.change(field, { target: { value: "1" } });
    fireEvent.keyDown(field, { key: "Escape" });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Tap to enter a frequency/ })).toBeInTheDocument(),
    );
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("which radios get a tab", () => {
  it("includes a radio the scan cannot see but a service is reserved for", () => {
    // A dedicated radio WAITS for its dongle rather than moving to another one, so it
    // is still the radio that job is on. Dropping it would hide the job entirely.
    const shown = tabbable({
      radios: [
        radio(WHIP, { attached: true }),
        radio(WIRE, { attached: false, role: "aprs" }),
        radio("00000000", { attached: false }),
      ],
      conflicts: {},
      scan_ok: true,
    });

    expect(shown.map((r) => r.serial)).toEqual([WHIP, WIRE]);
  });
});
