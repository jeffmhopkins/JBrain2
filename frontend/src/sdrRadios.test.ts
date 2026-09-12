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
  converterNote,
  gainNote,
  generalOutcome,
  isKnownRole,
  labelFor,
  outcomeFor,
  roleLabel,
} from "./sdrRadios";

const WHIP = "09022796";
const WIRE = "77192819";

function radio(serial: string, over: Partial<SdrRadio> = {}): SdrRadio {
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
      // Unset, not zero: 0 dB is a real gain this radio has run at, and a falsy default
      // would report a choice nobody made — and the one the measurements call deaf.
      gain: "",
      upconverter_hz: 0,
    });
  });

  it("reads a gain this build does not know as UNSET, not as a setting", () => {
    // A rung nobody measured must not reach a control that offers only measured ones,
    // and must certainly not be drawn as chosen. Unset is the state every box was in
    // before this field existed, so it is the safe place to land.
    for (const junk of ["25", "auto ", 30, null, {}]) {
      expect(asRadios({ radios: [{ serial: WHIP, gain: junk }] })?.radios[0]?.gain).toBe("");
    }
  });

  it("reads a junk or negative offset as no converter", () => {
    // The one fallback that cannot mis-tune: it is what the box did before the field
    // existed. A half-read offset would have the radio tune somewhere nobody asked for.
    for (const junk of ["125", -1, 0, Number.NaN, null]) {
      expect(
        asRadios({ radios: [{ serial: WHIP, upconverter_hz: junk }] })?.radios[0]?.upconverter_hz,
      ).toBe(0);
    }
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

describe("what the two new fields promise", () => {
  // Both notes are the only place the CONSEQUENCE of the setting is stated, so they are
  // tested as claims rather than as strings: what they must say, and what they must not.

  it("offers the measured rungs as measurements, never as a range", () => {
    // 0 is not the safe end of a slider — measured 20.2 dB SNR against 41.4 at 20 dB.
    expect(gainNote("0", false).text).toMatch(/deaf/);
    expect(gainNote("40", false).text).toMatch(/overloads/);
    expect(gainNote("20", false).text).toMatch(/plateau/);
  });

  it("says a gain cannot apply on the direct path, and stays quiet once it can", () => {
    expect(gainNote("20", false).text).toMatch(/tuner is powered down/);
    expect(gainNote("20", true).text).not.toMatch(/powered down/);
  });

  it("never names a passband edge for a converter nobody has measured", () => {
    // The open question the mock refuses to answer with a number it does not have.
    const said = converterNote(125_000_000).text;

    expect(said).toMatch(/passes HF only/);
    expect(said).not.toMatch(/\b(cut-?off|edge at)\b/i);
  });

  it("promises the owner's frequency back, which is the whole risk of the feature", () => {
    expect(converterNote(125_000_000).text).toMatch(/stays the real one/);
    expect(converterNote(0).text).toMatch(/sees the antenna directly/);
  });

  it("works the shift for whatever offset this unit turns out to have", () => {
    // 125 is this unit's number, not every unit's — the reason the field is editable.
    expect(converterNote(100_000_000).text).toMatch(/107\.200/);
  });
});
