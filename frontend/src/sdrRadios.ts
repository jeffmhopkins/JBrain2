// Which radio does what — the Settings screen for a box with more than one dongle.
//
// MEASURED 2026-09-03: two NESDR SMArt v5s attached, and both `rtl_fm` and `rtl_power`
// invoked with no `-d`, so they opened whichever librtlsdr enumerated first. With one
// radio on a desk whip and one on a long wire that is how APRS silently changes antenna
// — no error, no log line, just worse reception. Binding spec:
// docs/mocks/sdr-dongles/a-named-roles.html.
//
// Pure: the shapes the wire carries and the sentences the screen says about them. The
// decision itself is the backend's (`jbrain/sdr/roles.py`) and is NOT reimplemented here
// — a second copy of "dedicated does not fall back" would be a second thing to get wrong,
// and the two would disagree silently.

/** A radio the owner has described, the scan can see, or both. */
export interface SdrRadio {
  serial: string;
  name: string;
  description: string;
  /** `general`, or a service id this radio is reserved for. */
  role: string;
  attached: boolean;
  /** The tuner gain pinned on this radio: `""` unset, `auto`, or a measured rung in dB.
   *  Unset is the ABSENCE of a choice, not a zero — 0 dB is a real setting this radio
   *  has run at, and drawing unset as 0 would tell the owner they had chosen the one
   *  rung the measurements call deaf. */
  gain: string;
  /** How far a converter in front of this dongle shifts the hardware tune, in Hz. 0 is
   *  none. It never appears in a frequency anything displays. */
  upconverter_hz: number;
}

/** Hand the tuner back to its own loop. */
export const GAIN_AUTO = "auto";

/** The gain settings that were measured, and nothing between them, because nothing
 *  between them was measured. At 162.550 through the fixed listen chain: 0 dB gives
 *  20.2 dB SNR, 10/20/30 give 40.5/41.4/39.7, 40 gives 32.8 — a plateau at 10-30 with
 *  both ends worse. A slider would imply readings nobody took. */
export const GAIN_RUNGS: readonly string[] = ["0", "10", "20", "30", "40"];

/** The Nooelec Ham It Up's nominal crystal, in MHz — the value the field starts at when
 *  a converter is switched on. EDITABLE, and only a starting point: 125 is this unit's
 *  number, not every unit's, and nobody has measured this one. */
export const HAM_IT_UP_MHZ = 125;

export interface SdrRadios {
  radios: SdrRadio[];
  /** Service id → serials, for services with more than one radio dedicated to them. */
  conflicts: Record<string, string[]>;
  /** False when the USB scan could not be reached, so `attached` is unknown. */
  scan_ok: boolean;
}

export const GENERAL = "general";

/**
 * The services a radio can be reserved for, and what to call them.
 *
 * Only services that EXIST are offered. Reserving a radio for something the box cannot
 * run would take it away from the tuner in exchange for nothing — a setting whose only
 * effect is to lose you a radio.
 */
export const SERVICES: ReadonlyArray<{ id: string; label: string }> = [
  { id: "aprs", label: "APRS logging" },
];

/**
 * A wire payload, or null if it is not one.
 *
 * Defensive because this card renders inside the SETTINGS SCREEN: a malformed or absent
 * response must cost the owner this one card, never the screen they actually opened —
 * and "absent" is the normal answer on a box with no radio. The backend reads the stored
 * value with the same suspicion, for the same reason.
 */
export function asRadios(value: unknown): SdrRadios | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (!Array.isArray(raw.radios)) return null;
  const radios: SdrRadio[] = [];
  for (const entry of raw.radios) {
    if (typeof entry !== "object" || entry === null) continue;
    const row = entry as Record<string, unknown>;
    if (typeof row.serial !== "string" || row.serial === "") continue;
    radios.push({
      serial: row.serial,
      name: typeof row.name === "string" ? row.name : "",
      description: typeof row.description === "string" ? row.description : "",
      // An unreadable role reads as GENERAL here, unlike the backend, and the asymmetry
      // is deliberate: the backend decides who gets the radio and must not free a
      // reservation it cannot parse, while this only decides what a label says.
      role: typeof row.role === "string" && row.role !== "" ? row.role : GENERAL,
      attached: row.attached === true,
      // Unreadable reads as UNSET for both, which is the one fallback that cannot
      // mislead: it is what a box that never opened this screen does, and the card then
      // says so rather than showing a gain nothing is running at.
      gain:
        typeof row.gain === "string" && (row.gain === GAIN_AUTO || GAIN_RUNGS.includes(row.gain))
          ? row.gain
          : "",
      upconverter_hz:
        typeof row.upconverter_hz === "number" &&
        Number.isFinite(row.upconverter_hz) &&
        row.upconverter_hz > 0
          ? Math.round(row.upconverter_hz)
          : 0,
    });
  }
  const conflicts =
    typeof raw.conflicts === "object" && raw.conflicts !== null
      ? (raw.conflicts as Record<string, string[]>)
      : {};
  return { radios, conflicts, scan_ok: raw.scan_ok !== false };
}

/** What to call a radio in a sentence: the owner's name, or the serial if unnamed. */
export function labelFor(radio: SdrRadio): string {
  return radio.name.trim() || radio.serial;
}

/**
 * What the "Used for" control should say.
 *
 * An unrecognised role reads as dedicated to that id rather than as general use, because
 * that is what the backend does with it: an unknown role keeps the radio reserved. A UI
 * that displayed it as "general" would be describing a radio the tuner cannot actually
 * have.
 */
export function roleLabel(role: string): string {
  if (role === GENERAL) return "General use";
  const known = SERVICES.find((s) => s.id === role);
  return known ? `Dedicated — ${known.label}` : `Dedicated — ${role}`;
}

/** Whether a stored role is one this build can offer in the picker. */
export function isKnownRole(role: string): boolean {
  return role === GENERAL || SERVICES.some((s) => s.id === role);
}

/**
 * What will actually happen to one service, given what is described and attached.
 *
 * A READING of the backend's rule for the operator, not a second implementation of it:
 * it never decides anything, and the words it produces are about a state the API has
 * already committed to. `waiting` is the case worth showing loudly — a service whose
 * radio is unplugged does not move to another one, and without a sentence saying so the
 * only symptom is silence that looks like a quiet band.
 */
export function outcomeFor(
  state: SdrRadios,
  service: string,
): { tone: "ok" | "warn" | "bad"; text: string } {
  // With no scan, every radio arrives `attached: false` — and reading that literally
  // makes this say "Waiting for Desk whip … it will not move to another radio" in the
  // one state where the API does NOT wait: `_radio_for` returns `unknown` and lets the
  // sidecar open whatever it likes. Asserting the guarantee exactly where it is off is
  // worse than saying nothing.
  if (!state.scan_ok) {
    return { tone: "warn", text: "Unknown — the USB scan could not say what is attached." };
  }
  const dedicated = state.radios.filter((r) => r.role === service);
  const [reserved] = dedicated;
  const [live] = dedicated.filter((r) => r.attached);
  const [spare] = state.radios.filter((r) => r.role === GENERAL && r.attached);

  if (dedicated.length > 1) {
    return {
      tone: "bad",
      text: `${dedicated.map(labelFor).join(" and ")} are both dedicated to it — every frame would be logged twice.`,
    };
  }
  if (live !== undefined) {
    return { tone: "ok", text: `Uses ${labelFor(live)}. Reserved, so nothing else can take it.` };
  }
  if (reserved !== undefined) {
    return {
      tone: "bad",
      text: `Waiting for ${labelFor(reserved)} — it is dedicated to this and not attached. It will not move to another radio.`,
    };
  }
  if (spare !== undefined) {
    return {
      tone: "warn",
      text: `Uses ${labelFor(spare)} — nothing is dedicated to it, so the tuner can take that radio away.`,
    };
  }
  return { tone: "bad", text: "No radio available." };
}

/** What the tuner and sweeps are left with once services have taken theirs. */
export function generalOutcome(state: SdrRadios): { tone: "ok" | "warn" | "bad"; text: string } {
  // Same trap: without a scan this said "No radio attached." directly under a banner
  // admitting we cannot tell, while two dongles were plugged in.
  if (!state.scan_ok) {
    return { tone: "warn", text: "Unknown — the USB scan could not say what is attached." };
  }
  const generals = state.radios.filter((r) => r.role === GENERAL && r.attached);
  const [only] = generals;
  if (only === undefined) {
    return {
      tone: "bad",
      text: state.radios.some((r) => r.attached)
        ? "No radio available: every attached radio is dedicated to a service."
        : "No radio attached.",
    };
  }
  if (generals.length === 1) {
    return {
      tone: "ok",
      text: `Uses ${labelFor(only)} — one radio, so the tuner and a sweep still take turns.`,
    };
  }
  return { tone: "ok", text: `Uses ${generals.map(labelFor).join(" or ")}.` };
}

/** One line under a control: what it says, and how loudly. */
export interface Note {
  tone: "plain" | "warn" | "off";
  text: string;
}

/**
 * What the gain control says about the choice showing in it.
 *
 * Every sentence here is a MEASUREMENT on this radio at 162.550 through the fixed
 * listen chain, which is why the control offers rungs rather than a slider: 0 dB gives
 * 20.2 dB SNR, 10/20/30 give 40.5/41.4/39.7, 40 gives 32.8. Both ends are worse, the
 * bottom by ~20 dB, so 0 is not the safe end of a range — it is deaf.
 *
 * `converter` is whether one is inline, because it decides whether the control can
 * apply at all below 24 MHz: direct sampling powers the tuner down, and every gain
 * stage an R820T2 has is in the tuner. Saying so ALWAYS rather than only when a
 * shortwave frequency happens to be tuned is deliberate — this screen has no frequency,
 * and a caveat that appeared only sometimes would read as a fault rather than a fact.
 */
export function gainNote(gain: string, converter: boolean): Note {
  const hf = converter
    ? ""
    : " Below 24 MHz with no converter the radio direct-samples: the tuner is powered down, so " +
      "there is no gain stage and this cannot apply there. It is stored and applies the moment it " +
      "can.";
  if (gain === "") {
    return {
      tone: "off",
      text: `Unset — listening keeps the radio's own loop and a waterfall pins 30 dB, which is what this box has always done.${hf}`,
    };
  }
  if (gain === GAIN_AUTO) {
    return {
      tone: "warn",
      text: `Auto moves the gain while you watch. On this radio that put a station at 162.35 that does not exist, and a spur comb at ±55.5/111/166/222 kHz. The waterfall's dB scale becomes relative, and it will say so.${hf}`,
    };
  }
  if (gain === "0") {
    return {
      tone: "warn",
      text: `0 dB is not the safe end — it is deaf. Measured 20.2 dB SNR here against 41.4 at 20 dB. The useful range is 10–30.${hf}`,
    };
  }
  if (gain === "40") {
    return {
      tone: "warn",
      text: `40 dB overloads. Measured 32.8 dB SNR, below the 10–30 plateau. With an amp in front, come down rather than up.${hf}`,
    };
  }
  return {
    tone: "plain",
    text: `Measured plateau: 10/20/30 dB give 40.5/41.4/39.7 dB SNR here. With an inline amp, try 10 or 20 — the amp supplies gain the tuner then does not have to.${hf}`,
  };
}

/**
 * What the upconverter control says about the offset showing in it.
 *
 * **No passband edge is named.** Nobody has measured this unit's, so the warning is
 * that VHF will not reach the mixer with the converter inline rather than a number the
 * card cannot stand behind. The arithmetic is worked in the direction the owner will
 * feel it: they ask for a frequency, the dongle is told a higher one, and everything
 * they read stays where they asked.
 */
export function converterNote(upconverterHz: number): Note {
  if (upconverterHz <= 0) {
    return { tone: "off", text: "Off — the dongle sees the antenna directly." };
  }
  const mhz = upconverterHz / 1_000_000;
  const shown = mhz.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return {
    tone: "warn",
    text: `Hardware tunes ${shown} MHz above what you ask, so 7.200 MHz is heard at ${(7.2 + mhz).toFixed(3)}. Every frequency you read — waterfall, peaks, recordings, APRS — stays the real one. The converter passes HF only — 300 Hz to 65 MHz at its input — so switch the unit to bypass, or turn this off, before tuning VHF. Above 65 MHz with this on, the box refuses rather than tuning something you would not hear.`,
  };
}
