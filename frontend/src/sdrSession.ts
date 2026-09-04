// The live radio session, shared by every reader.
//
// The box holds one session per radio, and a session's EXISTENCE is what
// decides whether the omnibox shows a radio icon at all. Keeping that in one shared
// reading rather than per-component state is what stops the composer and the tuner
// sheet ever disagreeing about whether the radio is held
// (docs/plans/SDR_RADIO_PLAN.md D7, docs/mocks/sdr-tuner/a-tuner-sheet.html).
//
// Shaped after hostVitals' pub/sub: there is no React context anywhere in this app,
// and a module store with a subscribe hook is the established way a screen learns
// about live box state. Refcounted — the first reader starts polling, the last stops —
// so an app with no radio surface open costs nothing.

import { useEffect, useState } from "react";
import { ApiError, api } from "./api/client";
import { playSdrAudio, stopSdrAudio } from "./sdrAudio";

export interface SdrListening {
  session_id: string;
  frequency_hz: number;
  mode: string;
  gain: string | null;
  /** What the session is holding the tuner for — `listen` or `aprs`
   *  (deploy/sdr/listen.py). Absent on a sidecar older than purposes, which only ever
   *  listened. */
  purpose?: string;
  /** The box's clock when this session started. With `elapsed_s` it gives the box's
   *  clock NOW, which is what anchors the audio timeline for caption timing. */
  started_at: number;
  elapsed_s: number;
  peak: number;
  listeners: number;
}

export interface SdrState {
  /** False on a box with no radio, or one whose sidecar is unreachable. Either way
   *  the icon must not appear: a lit icon over a dead radio is worse than none. */
  available: boolean;
  /** Null when the radio is idle. Non-null is precisely the icon's condition.
   *
   *  ONE session — the omnibox draws one icon — and it PREFERS the tuner. So it answers
   *  "what should the icon show", never "is APRS logging": with a radio each, reading
   *  its purpose for that told the APRS tab nothing was logging while it was. Use
   *  `sessionFor` for a question about a particular job. */
  listening: SdrListening | null;
  /** Every radio the box is holding. Absent from an api older than per-radio sessions,
   *  hence the default — a box like that can hold only one thing anyway. */
  sessions?: SdrListening[];
}

/** The session holding a radio for one job, or null.
 *
 *  The whole reason `sessions` exists. `listening` is what to DRAW; this is what to
 *  ASK. Falls back to `listening` for an api that predates the field, which can only
 *  ever have had the one session. */
export function sessionFor(state: SdrState, purpose: string): SdrListening | null {
  const live = state.sessions ?? (state.listening ? [state.listening] : []);
  return live.find((s) => (s.purpose ?? "listen") === purpose) ?? null;
}

/** Whether a held radio is one there is any point hearing. A session whose purpose is
 *  anything but `listen` is decoding, not playing. Absent means a sidecar older than
 *  purposes, which only ever listened. */
export function isAudible(session: SdrListening): boolean {
  return (session.purpose ?? "listen") === "listen";
}

type Listener = (state: SdrState) => void;

const IDLE: SdrState = { available: false, listening: null, sessions: [] };
// A second is the same cadence the vitals stream uses, and the tuner shows an
// elapsed time and a level meter that both want to move.
const POLL_MS = 1000;

let published: SdrState = IDLE;
const listeners = new Set<Listener>();
let timer: number | null = null;
let inFlight = false;

function publish(next: SdrState): void {
  const was = published.listening?.session_id ?? null;
  const now = next.listening?.session_id ?? null;
  published = next;
  // Audio follows the LEASE, not the sheet: it starts when a session appears and
  // stops when it goes, so closing the tuner does not silence the radio (sdrAudio.ts).
  // A retune keeps the session id, so this deliberately does not fire on one — the
  // stream survives the sidecar relaunching its encoder underneath it.
  if (now !== was) {
    // Audio follows a LISTENING session only. A logging session holds the same lease
    // and appears here identically, but it exists to decode packets — playing it would
    // put 1200-baud squawk through the owner's speakers the moment logging started.
    const listening = next.listening;
    if (now && listening && isAudible(listening)) {
      // started_at + elapsed_s IS the box's clock, already arriving every second.
      playSdrAudio(listening.started_at + listening.elapsed_s);
    } else stopSdrAudio();
  }
  for (const listener of listeners) listener(next);
}

async function poll(): Promise<void> {
  if (inFlight) return; // a slow box must not stack requests
  inFlight = true;
  try {
    publish(await api.getSdrStatus());
  } catch (error) {
    // 401/403 means the session is gone or this principal may not see the radio —
    // either way stop claiming a radio we cannot see. Anything else is transient.
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      publish(IDLE);
    }
  } finally {
    inFlight = false;
  }
}

function start(): void {
  if (timer !== null) return;
  void poll();
  timer = window.setInterval(() => void poll(), POLL_MS);
}

function stop(): void {
  if (timer === null) return;
  window.clearInterval(timer);
  timer = null;
}

/** Subscribe to the shared reading; returns an unsubscribe. */
export function subscribeSdr(listener: Listener): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    start();
  } else {
    listener(published); // a late joiner gets the current reading, not a blank
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) stop();
  };
}

/** The live radio session, or an idle reading. One poll serves every reader. */
export function useSdrSession(): SdrState {
  const [state, setState] = useState<SdrState>(() => published);
  useEffect(() => subscribeSdr(setState), []);
  return state;
}

/** Test seam: forget the shared reading between cases. */
export function resetSdrSession(): void {
  stop();
  listeners.clear();
  published = IDLE;
  inFlight = false;
}
