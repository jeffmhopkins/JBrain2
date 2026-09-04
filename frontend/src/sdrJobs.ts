// What each radio is doing, and what it may be asked to do instead.
//
// Binding spec: docs/mocks/sdr-launcher/shapes.html shape A — the RADIO is the object,
// so its job is chosen inside it. This file is the sentences that surface says, and
// nothing else: it decides nothing, opens nothing, holds no state.
//
// **It is a reading of the backend's rules, not a second copy of them.** `roles.named`
// decides whether a radio the owner tapped may take a job, and the sidecar's lease
// decides who is refused for being busy; both answer for real, and both answer in
// sentences this screen shows verbatim. What is here exists so a button is DISABLED
// with a reason instead of tapped into a 409 — and if the two ever disagree, the
// backend is right and this is a stale label.

import { mhz } from "./mhz";
import type { SdrRadio, SdrRadios } from "./sdrRadios";
import { GENERAL, labelFor } from "./sdrRadios";
import type { SdrListening, SdrState } from "./sdrSession";

/** The jobs a radio can be given, in the order the control offers them. */
export const JOBS: ReadonlyArray<{ id: string; label: string }> = [
  { id: "listen", label: "Listen" },
  { id: "aprs", label: "APRS" },
  { id: "spectrum", label: "Spectrum" },
  { id: "idle", label: "Idle" },
];

/** What to call a job in a sentence. Falls back to the id, because a role this build
 *  does not recognise still reserves the radio — the same reason `roles.job_label`
 *  does it server-side. */
export function jobLabel(id: string): string {
  return JOBS.find((j) => j.id === id)?.label ?? id;
}

/** What job a session is holding a radio for. Absent means a sidecar older than
 *  purposes, which only ever listened. */
export function jobOf(session: SdrListening): string {
  return session.purpose ?? "listen";
}

/** The session holding one radio, or null.
 *
 *  Matched on serial, and a session with NO serial matches any radio: that is what a
 *  one-dongle box has always sent, and reading it as "belongs to nothing" would show
 *  such a box as idle while its radio was plainly held. */
export function sessionOn(state: SdrState, serial: string): SdrListening | null {
  const live = state.sessions ?? (state.listening ? [state.listening] : []);
  return live.find((s) => !s.serial || s.serial === serial) ?? null;
}

export interface StateLine {
  tone: "on" | "warn" | "bad" | "idle";
  text: string;
}

/** What a radio is doing, in one line, in the words the sidecar already uses.
 *
 *  "Unknown" and "not attached" are DIFFERENT answers and are kept apart, because the
 *  fixes are: one is a USB scan that could not be reached, the other is a dongle to
 *  plug in. Collapsing them is the mistake `sdrRadios.outcomeFor` had to be corrected
 *  for, in this same screen. */
export function stateLine(
  radio: SdrRadio,
  session: SdrListening | null,
  scanOk: boolean,
): StateLine {
  if (!scanOk) {
    return { tone: "warn", text: "Unknown — the USB scan could not say what is attached." };
  }
  if (!radio.attached) {
    const waiting =
      radio.role === GENERAL
        ? " Plug it in to use it."
        : ` ${jobLabel(radio.role)} is waiting for it — it will not move to another radio.`;
    return { tone: "bad", text: `Not attached.${waiting}` };
  }
  if (!session) return { tone: "idle", text: "Idle" };
  const sweep = session.sweep;
  switch (jobOf(session)) {
    case "listen":
      return {
        tone: "on",
        text: `Listening — ${mhz(session.frequency_hz)} ${session.mode.toUpperCase()}`,
      };
    case "aprs":
      return { tone: "on", text: `Logging APRS — ${mhz(session.frequency_hz)}` };
    case "spectrum":
      return {
        tone: "on",
        text: sweep
          ? `Watching ${mhz(sweep.start_hz)}–${mhz(sweep.stop_hz)}`
          : "Watching the spectrum",
      };
    case "survey":
      // A sweep is a RUN, not a resting mode: it ends by itself and frees the radio,
      // which is why it reads as a warning rather than a steady state.
      return {
        tone: "warn",
        text: sweep ? `Sweeping ${mhz(sweep.start_hz)}–${mhz(sweep.stop_hz)}` : "Sweeping",
      };
    default:
      return { tone: "on", text: "In use" };
  }
}

/** Why this radio cannot take this job, or null.
 *
 *  Dedication binds the TUNER too, which is the half that surprises people: a radio
 *  reserved for APRS is not one the waterfall may borrow because APRS happens to be
 *  idle. That is `roles.named`'s rule; this only says so before the tap.
 *
 *  BUSY is not a reason here, deliberately. A radio already doing something can be
 *  given a different job — that is what the control is for — and the only thing that
 *  cannot happen is two radios doing the SAME job, which the sidecar has no notion of
 *  and this does. */
export function jobAllowed(
  radios: SdrRadios,
  sdr: SdrState,
  radio: SdrRadio,
  job: string,
): string | null {
  if (job === "idle") return null;
  // With no scan every radio arrives `attached: false`, and refusing on that would
  // disable every control on a box with two dongles plugged in. The api does not refuse
  // here either: it passes the named radio through and lets the sidecar answer.
  if (!radios.scan_ok) return null;
  if (!radio.attached) return "not attached";
  if (radio.role !== GENERAL && radio.role !== job) {
    return `reserved for ${jobLabel(radio.role)}`;
  }
  const elsewhere = radios.radios.find((other) => {
    if (other.serial === radio.serial || !other.attached) return false;
    const held = sessionOn(sdr, other.serial);
    return held !== null && jobOf(held) === job;
  });
  return elsewhere ? `${labelFor(elsewhere)} is doing it` : null;
}
