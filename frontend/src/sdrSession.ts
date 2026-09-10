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
import { ApiError, type SdrRecording, api } from "./api/client";
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
  /** WHICH radio this session opened, when the api named one. Null on a one-dongle box,
   *  where "whichever librtlsdr enumerates first" was always the answer and still is. */
  serial?: string | null;
  /** The RANGE, for the two purposes that have one — a sweep and a live spectrum. Null
   *  for the purposes that really are a single frequency. `frequency_hz` carries the
   *  midpoint of this, which is the only frequency a span has. */
  sweep?: { start_hz: number; stop_hz: number; bin_hz: number; seconds: number } | null;
  /** Which engine the sidecar ACTUALLY started — `iq` when it is demodulating the
   *  samples itself, `rtl_fm` / `rtl_power` when a subprocess is doing it. Reported
   *  because the choice is made at runtime and can fall back (deploy/sdr/listen.py),
   *  and because only the `iq` engine can draw the channel it is listening to. Absent
   *  on a sidecar older than the demodulator. */
  engine?: string;
  /** The box's clock when this session started. With `elapsed_s` it gives the box's
   *  clock NOW, which is what anchors the audio timeline for caption timing. */
  started_at: number;
  elapsed_s: number;
  /** Loudest sample of the DEMODULATED AUDIO, 0..1 of full scale — **not a signal
   *  level**, and not comparable with the waterfall's dBFS, which is a different
   *  quantity in different units (SDR_IQ_SPECTRUM_PLAN F9). It was once drawn as a
   *  signal meter in the tuner sheet, which is precisely the reading it does not
   *  support: idle airband AM sits at 0.21 because the tuner's AGC amplifies its own
   *  noise. Nothing renders it today; the name is what keeps it that way. */
  audio_peak: number;
  listeners: number;
  /** How wide the demodulator's filter is, as a FULL channel width in Hz. Zero on a
   *  session with no channel (a spectrum stare), and absent on a sidecar older than the
   *  bandwidth control — both of which mean "draw no bandwidth control". */
  bandwidth_hz?: number;
  /** Every width THIS mode offers, widest first. Sent WITH the session rather than
   *  hardcoded here, because the ladder is a property of the demodulator: a copy in the
   *  PWA would go on offering widths a redeployed box had stopped accepting, and the
   *  refusal would arrive as a 400 the owner cannot act on. */
  bandwidths_hz?: number[];
  /** The narrowest and widest this mode will build, and the grid a width must land on.
   *  The presets above are quick picks INSIDE this; the drag on the tuning view runs
   *  anywhere in the range, so the control needs the bounds rather than just the menu. */
  bandwidth_min_hz?: number;
  bandwidth_max_hz?: number;
  bandwidth_step_hz?: number;
  /** How wide the TUNING PICTURE is drawn, and the widths this mode offers. A different
   *  thing from the bandwidth above: that is what the radio hears, this is only how much
   *  spectrum is drawn around it. Changing it rebuilds nothing and never clicks the
   *  audio, which is why it has its own route rather than being a `tune` parameter. */
  view_span_hz?: number;
  view_spans_hz?: number[];
}

/** A capture in flight, as `GET /sdr/status` reports it (docs/plans/SDR_RECORDING_PLAN.md
 *  §4). The Record button draws its elapsed time and running size FROM HERE, off the 1 Hz
 *  poll every other radio reading already uses — a second timer in the component would be
 *  a clock of its own to drift against the box, and an optimistic local "recording" state
 *  would keep counting through a capture the box had already dropped. */
export interface SdrRecordingState {
  /** When the capture began, as the box's clock said it (`sdr/recorder.py`). */
  started_at: string;
  /** How long it has been running. Named as the recorder names it, not `elapsed_s` —
   *  the session's elapsed time is a different clock on the same poll, and one name for
   *  two quantities is how a surface ends up printing the wrong one. */
  seconds: number;
  /** What has landed in the blob so far. Reported rather than derived from the bitrate:
   *  the running size is the argument for stopping, so it has to be measured. */
  bytes: number;
  /** The settings the clip will carry. A retune does not restart the pipeline, so these
   *  are where the recording BEGAN, which is what the library will show. */
  frequency_hz: number;
  mode: string;
  bandwidth_hz: number | null;
  serial: string | null;
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
  /** The capture in flight, or null. One at a time, box-wide (`sdr/recorder.py`), which
   *  is why it sits on the STATUS rather than on a session. */
  recording?: SdrRecordingState | null;
}

/** The capture in flight, or null.
 *
 *  One reading, like `sessionFor` and `anyHeld`: the recorder is box-wide rather than a
 *  property of a session, so "is anything recording" is a question about the STATUS —
 *  and both the Record button and the library ask it of the same 1 Hz poll. */
export function liveRecording(state: SdrState): SdrRecordingState | null {
  return state.recording ?? null;
}

/** The session holding a radio for one job, or null.
 *
 *  The whole reason `sessions` exists. `listening` is what to DRAW; this is what to
 *  ASK. Falls back to `listening` for an api that predates the field, which can only
 *  ever have had the one session. */
export function sessionFor(state: SdrState, purpose: string): SdrListening | null {
  return liveSessions(state).find((s) => (s.purpose ?? "listen") === purpose) ?? null;
}

/** Every session the box is holding. One reading of the `sessions`-or-`listening`
 *  fallback, so a caller cannot forget the older api and see an idle box. */
export function liveSessions(state: SdrState): SdrListening[] {
  return state.sessions ?? (state.listening ? [state.listening] : []);
}

/** Whether the box is holding a radio at all — the omnibox icon's condition.
 *
 *  NOT `listening !== null`, which is the one session the icon DRAWS and prefers the
 *  tuner: a box whose only radio was decoding APRS or sweeping a spectrum showed no
 *  icon, so the sheet that can now control those jobs had no way in
 *  (docs/mocks/omnibox-radios/README.md). */
export function anyHeld(state: SdrState): boolean {
  return liveSessions(state).length > 0;
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
  savedListeners.clear();
  published = IDLE;
  inFlight = false;
}

// --- the row a stop just landed ------------------------------------------------------
//
// `liveRecording` above cannot answer "has the clip been written yet". The recorder
// reports no capture the moment the STREAM ends, which is before the waveform is
// computed and the row inserted, so a library reloading off that poll can legitimately
// read a list that does not contain the recording the owner just made — and nothing
// would ever re-read it. `POST /sdr/record?on=false` answers with the saved row itself,
// which is the only signal that is true by construction; this carries it from the
// control that pressed Stop to the tab that lists it, the two being on different tabs of
// the launcher and never mounted together.
//
// A one-shot signal, deliberately NOT retained state: a last-saved row kept around would
// be re-folded into the list by a library mounted long afterwards, resurrecting a
// recording the owner had since deleted.

type SavedListener = (row: SdrRecording) => void;

const savedListeners = new Set<SavedListener>();

/** Announce the row a just-completed capture landed. */
export function noteSdrRecordingSaved(row: SdrRecording): void {
  for (const listener of savedListeners) listener(row);
}

/** Hear about a capture that has finished being written; returns an unsubscribe. */
export function onSdrRecordingSaved(listener: SavedListener): () => void {
  savedListeners.add(listener);
  return () => {
    savedListeners.delete(listener);
  };
}
