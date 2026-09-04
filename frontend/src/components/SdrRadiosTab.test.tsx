// The Radios tab: the roster, and the job control inside a radio.
//
// Shape A makes the RADIO the object, so this is where every "which radio is doing
// what" question now lands — including the ones that used to live in the Tuner and
// APRS tabs. The cases worth keeping are the ones where the screen could lie about a
// radio: a job offered on a radio that may not take it, a switch that silently stops
// something the owner armed, and a tap the api quietly serves from a different dongle.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { resetBands } from "../sdrBands";
import { resetSdrSession } from "../sdrSession";
import { resetSdrSpectrum } from "../sdrSpectrum";
import { SdrRadiosTab } from "./SdrRadiosTab";

const WHIP = "09022796";
const WIRE = "77192819";

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
  ],
};

function radio(serial: string, over: Record<string, unknown> = {}) {
  return { serial, name: "", description: "", role: "general", attached: true, ...over };
}

function session(over: Record<string, unknown> = {}) {
  return {
    session_id: "s1",
    frequency_hz: 144_390_000,
    mode: "fm",
    gain: null,
    started_at: 0,
    elapsed_s: 30,
    peak: 0,
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

function show() {
  return render(<SdrRadiosTab tick={0} log={null} onOpenAprs={() => {}} />);
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

describe("the roster", () => {
  it("says what each radio is doing, per radio", async () => {
    // The bug this whole round exists to stop: `listening` is the ONE session the
    // omnibox draws and it prefers the tuner, so a screen reading it was told
    // "listening" while APRS logged on the other dongle.
    box(
      [radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })],
      [
        session({
          session_id: "s-tuner",
          purpose: "listen",
          serial: WHIP,
          frequency_hz: 99_300_000,
        }),
        session({ session_id: "s-aprs", purpose: "aprs", serial: WIRE }),
      ],
    );

    show();

    expect(await screen.findByText(/Listening — 99.300/)).toBeInTheDocument();
    expect(screen.getByText(/Logging APRS — 144.390/)).toBeInTheDocument();
  });

  it("says a box with no radio has none, rather than showing nothing", async () => {
    box([]);

    show();

    expect(await screen.findByText(/No radio on this box/)).toBeInTheDocument();
  });

  it("says the scan could not see, rather than that nothing is attached", async () => {
    // Read literally every radio arrives `attached: false`, and saying so under a
    // banner admitting we cannot see is the mistake `outcomeFor` was corrected for.
    vi.spyOn(api, "getSdrRadios").mockResolvedValue({
      radios: [radio(WHIP, { name: "Desk whip", attached: false })],
      conflicts: {},
      scan_ok: false,
    } as never);
    vi.spyOn(api, "getSdrStatus").mockResolvedValue({
      available: true,
      listening: null,
      sessions: [],
    } as never);

    show();

    expect(await screen.findByText(/unknown/)).toBeInTheDocument();
  });
});

async function open(name: string) {
  fireEvent.click(await screen.findByText(name));
}

describe("giving a radio a job", () => {
  it("names the radio the owner tapped, never whichever one is free", async () => {
    // The whole point of shape A. Without the serial the api takes `generals[0]`, and a
    // tap on the second card would quietly start the job on the first.
    box([radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })]);
    const start = vi.spyOn(api, "sdrSpectrumStart").mockResolvedValue(session() as never);

    show();
    await open("Long wire");
    fireEvent.click(screen.getByRole("button", { name: "Spectrum" }));
    fireEvent.click(await screen.findByText(/FM broadcast · The dial/));

    await waitFor(() => expect(start).toHaveBeenCalledWith({ section: "fm-broadcast" }, WIRE));
  });

  it("mounts the real transport for a listening radio, not a description of it", async () => {
    box([radio(WHIP, { name: "Desk whip" })], [session({ purpose: "listen", serial: WHIP })]);

    show();
    await open("Desk whip");

    // The same component the omnibox sheet opens, so the two cannot drift apart.
    expect(await screen.findByRole("button", { name: "Release" })).toBeInTheDocument();
  });

  it("takes a band before listening, because a frequency is not a thing to invent", async () => {
    box([radio(WHIP, { name: "Desk whip" })]);
    const listen = vi.spyOn(api, "sdrListen").mockResolvedValue(session() as never);

    show();
    await open("Desk whip");
    fireEvent.click(screen.getByRole("button", { name: "Listen" }));
    fireEvent.click(await screen.findByText(/FM broadcast · The dial/));

    // The section's own centre and mode — the settings someone chose while reading a
    // band plan, not a default this screen made up.
    await waitFor(() => expect(listen).toHaveBeenCalledWith(98, "wbfm", WHIP));
  });

  it("asks twice before stopping something that is running", async () => {
    // DESIGN.md: destructive actions get an inline confirm. One of the things a job
    // change stops is an APRS log the owner may have armed on a schedule — the silent
    // loss the sidecar's own `_stop` is written against.
    box([radio(WHIP, { name: "Desk whip" })], [session({ purpose: "aprs", serial: WHIP })]);
    const stop = vi.spyOn(api, "sdrStop").mockResolvedValue(undefined as never);

    show();
    await open("Desk whip");
    fireEvent.click(await screen.findByRole("button", { name: "Idle" }));

    expect(stop).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/that stops aprs on this radio/i);

    fireEvent.click(screen.getByRole("button", { name: "Again?" }));
    await waitFor(() => expect(stop).toHaveBeenCalledWith("s1"));
  });

  it("frees the radio before handing it to another job", async () => {
    // Otherwise the api's own lease refuses the change with a 409 naming the job the
    // owner has just asked to replace.
    box([radio(WHIP, { name: "Desk whip" })], [session({ purpose: "listen", serial: WHIP })]);
    const stop = vi.spyOn(api, "sdrStop").mockResolvedValue(undefined as never);
    const aprs = vi
      .spyOn(api, "setAprsLogging")
      .mockResolvedValue({ logging: true, changed: true } as never);

    show();
    await open("Desk whip");
    fireEvent.click(await screen.findByRole("button", { name: "APRS" }));
    fireEvent.click(screen.getByRole("button", { name: "Again?" }));

    await waitFor(() => expect(aprs).toHaveBeenCalledWith(true, undefined, WHIP));
    expect(stop).toHaveBeenCalledWith("s1");
  });

  it("will not put the same job on two radios at once, and says which has it", async () => {
    box(
      [radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })],
      [session({ purpose: "aprs", serial: WHIP })],
    );

    show();
    await open("Long wire");

    const aprs = await screen.findByRole("button", { name: "APRS" });
    expect(aprs.hasAttribute("disabled")).toBe(true);
    // Under the control, not only in a tooltip: a disabled button on a phone has no
    // hover, so a `title` alone is a reason the owner can never read.
    expect(screen.getByText(/Desk whip is doing it/)).toBeInTheDocument();
  });

  it("holds a dedicated radio for its own service, tuner included", async () => {
    box([radio(WHIP, { name: "Desk whip", role: "aprs" })]);

    show();
    await open("Desk whip");

    expect((await screen.findByRole("button", { name: "Listen" })).hasAttribute("disabled")).toBe(
      true,
    );
    expect(screen.getByText(/reserved for APRS/)).toBeInTheDocument();
  });

  it("says a dedicated radio is waited FOR, not replaced", async () => {
    box([radio(WHIP, { name: "Desk whip", role: "aprs", attached: false })]);

    show();
    await open("Desk whip");

    expect(await screen.findByText(/will wait for it rather than moving/)).toBeInTheDocument();
  });

  it("surfaces the api's own refusal rather than a generic failure", async () => {
    // The 409 names the radio, the job holding it, or why this radio may not have this
    // one. All three are things only the owner can act on.
    box([radio(WHIP, { name: "Desk whip" })]);
    vi.spyOn(api, "sdrSpectrumStart").mockRejectedValue(
      new ApiError(409, "Long wire is reserved for APRS logging."),
    );

    show();
    await open("Desk whip");
    fireEvent.click(screen.getByRole("button", { name: "Spectrum" }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByText(/FM broadcast · The dial/));

    expect(await screen.findByText(/reserved for APRS logging/)).toBeInTheDocument();
  });
});
