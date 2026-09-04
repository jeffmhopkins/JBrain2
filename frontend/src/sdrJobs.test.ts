// What the radio roster says, and which buttons it will not let you press.
//
// Every case here is a sentence the owner reads off a card. The two that matter most
// are the ones that look alike and are not: a radio we cannot see, and a radio that is
// not there. One is a scan to fix, the other is a dongle to plug in.

import { describe, expect, it } from "vitest";
import { jobAllowed, jobOf, sessionOn, stateLine } from "./sdrJobs";
import type { SdrRadio, SdrRadios } from "./sdrRadios";
import type { SdrListening, SdrState } from "./sdrSession";

const WHIP = "09022796";
const WIRE = "77192819";

function radio(serial: string, over: Partial<SdrRadio> = {}): SdrRadio {
  return { serial, name: "", description: "", role: "general", attached: true, ...over };
}

function radios(list: SdrRadio[], scanOk = true): SdrRadios {
  return { radios: list, conflicts: {}, scan_ok: scanOk };
}

function session(over: Partial<SdrListening> = {}): SdrListening {
  return {
    session_id: "s1",
    frequency_hz: 144_390_000,
    mode: "fm",
    gain: null,
    started_at: 0,
    elapsed_s: 5,
    peak: 0,
    listeners: 0,
    ...over,
  };
}

function state(sessions: SdrListening[]): SdrState {
  return { available: true, listening: sessions[0] ?? null, sessions };
}

describe("what a radio is doing", () => {
  it("says what a session is holding it for, in the sidecar's own words", () => {
    expect(stateLine(radio(WHIP), session({ purpose: "aprs" }), true).text).toContain(
      "Logging APRS — 144.390",
    );
    expect(
      stateLine(
        radio(WHIP),
        session({ purpose: "listen", frequency_hz: 99_300_000, mode: "wbfm" }),
        true,
      ).text,
    ).toBe("Listening — 99.300 WBFM");
  });

  it("names the RANGE for the two jobs that have one", () => {
    const sweep = { start_hz: 144_000_000, stop_hz: 148_000_000, bin_hz: 25_000, seconds: 60 };

    // `frequency_hz` carries only the midpoint, which reads as a tuner parked somewhere
    // it is not — the reason the range is on the wire at all.
    expect(stateLine(radio(WHIP), session({ purpose: "spectrum", sweep }), true).text).toBe(
      // Three decimals, as everywhere else in the app (`mhz.ts`): the channel spacing
      // on most of the narrowband plan is 5 kHz, so a second decimal is a digit short
      // of naming a channel.
      "Watching 144.000–148.000",
    );
    // A sweep is a RUN: it ends by itself and frees the radio, so it is not a steady
    // state and does not read as one.
    expect(stateLine(radio(WHIP), session({ purpose: "survey", sweep }), true).tone).toBe("warn");
  });

  it("keeps a radio we cannot see apart from one that is not there", () => {
    // Same card, opposite fixes: a scan to repair, or a dongle to plug in. Collapsing
    // them told the owner "not attached" about two dongles sitting on the desk.
    expect(stateLine(radio(WHIP), null, false).tone).toBe("warn");
    expect(stateLine(radio(WHIP, { attached: false }), null, true).tone).toBe("bad");
  });

  it("says a dedicated radio is being waited FOR, not replaced", () => {
    const line = stateLine(radio(WHIP, { attached: false, role: "aprs" }), null, true);

    expect(line.text).toContain("will not move to another radio");
  });

  it("reads a session with no serial as belonging to whichever radio is asked", () => {
    // What a one-dongle box has always sent. Read as "belongs to nothing", every such
    // box would show as idle while its radio was plainly held.
    expect(sessionOn(state([session()]), WHIP)).not.toBeNull();
    expect(sessionOn(state([session({ serial: WIRE })]), WHIP)).toBeNull();
    expect(jobOf(session())).toBe("listen");
  });
});

describe("which jobs a radio may take", () => {
  it("lets a general radio take anything", () => {
    const box = radios([radio(WHIP)]);

    expect(jobAllowed(box, state([]), radio(WHIP), "listen")).toBeNull();
    expect(jobAllowed(box, state([]), radio(WHIP), "spectrum")).toBeNull();
  });

  it("holds a dedicated radio for its own service, tuner included", () => {
    // The half that surprises people: a radio kept for APRS is not one the waterfall
    // may borrow because APRS happens to be idle.
    const dedicated = radio(WHIP, { role: "aprs" });
    const box = radios([dedicated]);

    expect(jobAllowed(box, state([]), dedicated, "listen")).toContain("reserved for APRS");
    expect(jobAllowed(box, state([]), dedicated, "aprs")).toBeNull();
  });

  it("will not put the same job on two radios at once", () => {
    const box = radios([radio(WHIP, { name: "Desk whip" }), radio(WIRE, { name: "Long wire" })]);
    const held = state([session({ purpose: "aprs", serial: WHIP })]);

    expect(jobAllowed(box, held, radio(WIRE), "aprs")).toBe("Desk whip is doing it");
    // ...but a radio already busy may still be given a different job. That is what the
    // control is for; the only impossible thing is two radios doing the same one.
    expect(jobAllowed(box, held, radio(WIRE), "listen")).toBeNull();
  });

  it("refuses a radio that is not attached", () => {
    const gone = radio(WIRE, { attached: false });

    expect(jobAllowed(radios([gone]), state([]), gone, "listen")).toBe("not attached");
  });

  it("disables nothing when the scan could not see", () => {
    // Every radio arrives `attached: false` then, and refusing on it would grey out the
    // whole screen on a box with two dongles plugged in. The api does not refuse here
    // either — it passes the named radio through and lets the sidecar answer.
    const box = radios([radio(WHIP, { attached: false })], false);

    expect(jobAllowed(box, state([]), radio(WHIP, { attached: false }), "listen")).toBeNull();
  });

  it("never refuses idle", () => {
    // Releasing a radio has to work whatever else is true of it.
    const gone = radio(WIRE, { attached: false, role: "aprs" });

    expect(jobAllowed(radios([gone]), state([]), gone, "idle")).toBeNull();
  });
});
