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
}

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
