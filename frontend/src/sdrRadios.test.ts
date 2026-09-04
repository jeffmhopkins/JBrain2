// What the Radios screen SAYS about a set of radios.
//
// The sentences matter as much as the routing: a service whose dedicated radio is
// unplugged does not move to another one, and if the screen does not say so the only
// symptom is silence that reads like a quiet band. The routing itself is the backend's
// (`jbrain/sdr/roles.py`) and is deliberately not reimplemented here.

import { describe, expect, it } from "vitest";
import {
  type SdrRadio,
  type SdrRadios,
  asRadios,
  generalOutcome,
  isKnownRole,
  labelFor,
  outcomeFor,
  roleLabel,
} from "./sdrRadios";

const WHIP = "09022796";
const WIRE = "77192819";

function radio(serial: string, over: Partial<SdrRadio> = {}): SdrRadio {
  return { serial, name: "", description: "", role: "general", attached: true, ...over };
}

function state(radios: SdrRadio[], over: Partial<SdrRadios> = {}): SdrRadios {
  return { radios, conflicts: {}, scan_ok: true, ...over };
}

describe("naming a radio", () => {
  it("uses the owner's name", () => {
    expect(labelFor(radio(WHIP, { name: "Desk whip" }))).toBe("Desk whip");
  });

  it("falls back to the serial, because an unnamed radio still has to be nameable", () => {
    expect(labelFor(radio(WHIP))).toBe(WHIP);
    expect(labelFor(radio(WHIP, { name: "   " }))).toBe(WHIP);
  });
});

describe("what a role is called", () => {
  it("names the services this build knows", () => {
    expect(roleLabel("general")).toBe("General use");
    expect(roleLabel("aprs")).toBe("Dedicated — APRS logging");
  });

  it("shows an unknown role as DEDICATED, not as general", () => {
    // The backend keeps a radio with an unknown role reserved. Drawing it as general
    // would describe a radio the tuner cannot actually have — and would invite the
    // owner to hand it out twice.
    expect(roleLabel("shortwave")).toBe("Dedicated — shortwave");
    expect(isKnownRole("shortwave")).toBe(false);
    expect(isKnownRole("aprs")).toBe(true);
  });
});

describe("what will actually happen", () => {
  it("says which radio a service holds when it has one", () => {
    const out = outcomeFor(state([radio(WIRE, { name: "Long wire", role: "aprs" })]), "aprs");

    expect(out.tone).toBe("ok");
    expect(out.text).toContain("Long wire");
  });

  it("says a service is WAITING when its radio is unplugged, and that it will not move", () => {
    // The sentence the whole feature exists for. A general radio is attached, so a
    // fallback is available and would be silent.
    const out = outcomeFor(
      state([
        radio(WHIP, { name: "Desk whip", role: "aprs", attached: false }),
        radio(WIRE, { name: "Long wire" }),
      ]),
      "aprs",
    );

    expect(out.tone).toBe("bad");
    expect(out.text).toContain("Waiting for Desk whip");
    expect(out.text).toContain("will not move");
  });

  it("warns that a shared radio can be taken away", () => {
    const out = outcomeFor(state([radio(WIRE, { name: "Long wire" })]), "aprs");

    expect(out.tone).toBe("warn");
    expect(out.text).toContain("take that radio away");
  });

  it("calls out two radios dedicated to one service", () => {
    const out = outcomeFor(
      state([
        radio(WHIP, { name: "Desk whip", role: "aprs" }),
        radio(WIRE, { name: "Long wire", role: "aprs" }),
      ]),
      "aprs",
    );

    expect(out.tone).toBe("bad");
    expect(out.text).toContain("logged twice");
  });
});

describe("what the tuner is left with", () => {
  it("says when every attached radio is spoken for", () => {
    const out = generalOutcome(state([radio(WHIP, { name: "Desk whip", role: "aprs" })]));

    expect(out.tone).toBe("bad");
    expect(out.text).toContain("every attached radio is dedicated");
  });

  it("distinguishes nothing-attached from everything-reserved", () => {
    const out = generalOutcome(state([radio(WHIP, { role: "aprs", attached: false })]));

    expect(out.text).toBe("No radio attached.");
  });

  it("admits that one shared radio still means taking turns", () => {
    const out = generalOutcome(state([radio(WIRE, { name: "Long wire" })]));

    expect(out.text).toContain("take turns");
  });
});

describe("reading the wire defensively", () => {
  it("survives a response that is not a payload at all", () => {
    // The failure that found this: SettingsScreen's own tests stub the api without
    // this method, so the card received `undefined` and took the whole screen down
    // with it. This card must never cost the owner the screen they actually opened.
    expect(asRadios(undefined)).toBeNull();
    expect(asRadios(null)).toBeNull();
    expect(asRadios("nope")).toBeNull();
    expect(asRadios({})).toBeNull();
  });

  it("drops rows with no serial rather than rendering a nameless radio", () => {
    const out = asRadios({ radios: [{ serial: "" }, { name: "x" }, { serial: WHIP }] });

    expect(out?.radios.map((r) => r.serial)).toEqual([WHIP]);
  });

  it("fills missing fields instead of failing", () => {
    const out = asRadios({ radios: [{ serial: WHIP }] });

    expect(out?.radios[0]).toEqual({
      serial: WHIP,
      name: "",
      description: "",
      role: "general",
      attached: false,
    });
  });

  it("treats a missing scan_ok as ok, so one absent field does not blank every row", () => {
    expect(asRadios({ radios: [] })?.scan_ok).toBe(true);
    expect(asRadios({ radios: [], scan_ok: false })?.scan_ok).toBe(false);
  });
});

describe("when the scan could not look", () => {
  it("does not claim a service is waiting, because the API is not waiting", () => {
    // With no scan every radio arrives attached:false. Read literally that produced
    // "Waiting for Desk whip … it will not move to another radio" in the exact state
    // where `_radio_for` returns `unknown` and lets the sidecar open whatever it likes.
    const out = outcomeFor(
      state([radio(WHIP, { name: "Desk whip", role: "aprs", attached: false })], {
        scan_ok: false,
      }),
      "aprs",
    );

    expect(out.text).toContain("Unknown");
    expect(out.text).not.toContain("Waiting");
  });

  it("does not say no radio is attached while it cannot tell", () => {
    const out = generalOutcome(state([radio(WHIP, { attached: false })], { scan_ok: false }));

    expect(out.text).toContain("Unknown");
    expect(out.text).not.toContain("No radio attached");
  });
});
