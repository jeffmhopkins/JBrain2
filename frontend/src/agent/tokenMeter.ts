// Live generation rate for the top bar, measured off the chat stream the PWA is
// already reading. No new endpoint and no extra load on a working turn: every
// answer/reasoning delta that arrives is weighed here on its way to the bubble.
//
// Module-scoped rather than React state, for two reasons. The turn runs DETACHED
// server-side and its stream is folded by the hook inside HomeScreen, which stays
// mounted across sub-screens — but the TopBar reading it renders on every screen,
// including overlays that hook never sees. And sub-agent output counts toward the
// rate, so a deep-research fan (where the parent sits idle for minutes while its
// children stream) still reads as the box working, which is the whole point of a
// gauge in the chrome. Same bus pattern as readAloudBus.ts.
//
// The rate is an ESTIMATE. It divides streamed characters by the same
// _CHARS_PER_TOKEN = 4 the backend's live usage meter uses (agent/loop.py), so the
// two surfaces never disagree with each other. Real tokenizer counts arrive only
// at step end, far too coarse for a once-a-second reading.

import { useEffect, useState } from "react";

/** Matches `_CHARS_PER_TOKEN` in backend/src/jbrain/agent/loop.py — keep in step so
 *  the top bar and the composer's context meter estimate tokens the same way. */
const CHARS_PER_TOKEN = 4;
/** Rolling window the rate averages over. Long enough that one chunky delta doesn't
 *  spike the reading, short enough to track a model that slows down mid-answer. */
const WINDOW_MS = 3000;
/** Silence after which the turn counts as over. A model between tool calls can go
 *  quiet for a second or two without the reading dropping out. */
const IDLE_MS = 2500;
/** The publish cadence the owner asked for. */
const TICK_MS = 1000;
/** Floor on the divisor for the first reading of a turn: dividing by a 200ms span
 *  would print a wild number before the window fills. */
const MIN_SPAN_MS = 800;

interface Sample {
  at: number;
  chars: number;
}

type Listener = (rate: number | null) => void;

let samples: Sample[] = [];
let streamStart = 0;
let timer: ReturnType<typeof setInterval> | null = null;
let published: number | null = null;
const listeners = new Set<Listener>();

function emit(rate: number | null): void {
  published = rate;
  for (const listener of listeners) listener(rate);
}

/** Tokens/sec over the trailing window, or null when nothing has streamed recently.
 *  The divisor is the window's *filled* span, so a turn's first reading is honest
 *  about the little time it has actually been running. */
function measure(now: number): number | null {
  const cutoff = now - WINDOW_MS;
  samples = samples.filter((s) => s.at > cutoff);
  const last = samples[samples.length - 1];
  if (last === undefined || now - last.at > IDLE_MS) return null;
  const chars = samples.reduce((total, s) => total + s.chars, 0);
  const span = Math.max(MIN_SPAN_MS, Math.min(WINDOW_MS, now - streamStart));
  return Math.round((chars / CHARS_PER_TOKEN / span) * 1000);
}

function tick(): void {
  const rate = measure(Date.now());
  if (rate === null) {
    stop();
    if (published !== null) emit(null);
    return;
  }
  emit(rate);
}

function stop(): void {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

/** Weigh a slice of streamed text. Called for every answer and reasoning delta of
 *  the owner's turn AND of any running sub-agent — the gauge answers "is the box
 *  generating", not "is this one bubble growing". */
export function recordStreamedText(text: string): void {
  if (!text) return;
  const now = Date.now();
  // A gap longer than the idle window starts a new measurement rather than
  // averaging across the pause, which would report a rate no model ever ran at.
  const last = samples[samples.length - 1];
  if (last === undefined || now - last.at > IDLE_MS) {
    samples = [];
    streamStart = now;
  }
  samples.push({ at: now, chars: text.length });
  if (timer === null) {
    timer = setInterval(tick, TICK_MS);
    tick(); // publish the first reading now rather than a second from now
  }
}

/** Drop the reading immediately — the turn settled, was stopped, or errored. Without
 *  this the gauge would coast for a further IDLE_MS after the last token. */
export function endTurnRate(): void {
  samples = [];
  stop();
  if (published !== null) emit(null);
}

/** Current published rate, for a component mounting mid-turn. */
export function currentTokenRate(): number | null {
  return published;
}

/** Subscribe to the 1 Hz rate; returns an unsubscribe. */
export function subscribeTokenRate(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The live generation rate in tokens/sec, or null when no turn is producing. */
export function useTokenRate(): number | null {
  const [rate, setRate] = useState<number | null>(currentTokenRate);
  useEffect(() => subscribeTokenRate(setRate), []);
  return rate;
}
