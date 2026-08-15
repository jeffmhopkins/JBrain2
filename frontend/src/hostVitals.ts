// The box's live GPU reading, behind the top bar's vitals trace.
//
// ONE stream for the whole app, module-scoped. The TopBar renders on every screen and
// App.tsx keeps HomeScreen mounted behind an open card, so several TopBars are alive
// at once — a per-component hook opened a separate EventSource for each, and they
// then disagreed: whichever instance's stream had failed showed "no gpu" while
// another showed 94%. Worse, browsers cap concurrent connections per origin, so with
// a chat turn streaming and a log tail open, one of those duplicate streams simply
// never reconnected and stayed dead for the rest of the session. Same bus pattern as
// agent/tokenMeter.ts.
//
// Access is probed once with a normal fetch before any stream opens. EventSource
// cannot see a status code — a rejected stream surfaces only as an error, and it
// retries forever — so a family member (whom the owner-only ops router rejects) would
// otherwise sit in a silent reconnect loop for the whole session.
//
// The stream is torn down whenever the app is backgrounded, or when the last reader
// goes away. A phone in a pocket has no business holding a socket open to read a
// gauge nobody is looking at (see visibility.ts).

import { useEffect, useState } from "react";

import { ApiError, api } from "./api/client";
import { isForeground } from "./visibility";

/** How long to wait before re-probing after a failure that might pass — a server
 *  mid-restart, a dropped network. A rejection is never retried; see `access`. */
const REPROBE_MS = 30_000;

/** Why there is or isn't a number.
 *  - `reading` — a live figure from the box.
 *  - `absent`  — the box answered, and it has no amdgpu gauge to report.
 *  - `unknown` — nobody has told us yet: connecting, reconnecting, or backgrounded.
 *
 *  `absent` and `unknown` were both `null` once, which made a dropped stream render
 *  as "no gpu" — claiming the hardware is missing when only the connection is. */
export type GaugeState = "reading" | "absent" | "unknown";

export interface GpuBusy {
  /** Busy percent, or null in both non-reading states. */
  percent: number | null;
  state: GaugeState;
}

const UNKNOWN: GpuBusy = { percent: null, state: "unknown" };

type Listener = (busy: GpuBusy) => void;
type Access = "unknown" | "allowed" | "denied";

const listeners = new Set<Listener>();
let source: EventSource | null = null;
let access: Access = "unknown";
let published: GpuBusy = UNKNOWN;
let reprobe: ReturnType<typeof setTimeout> | null = null;

function publish(busy: GpuBusy): void {
  published = busy;
  for (const listener of listeners) listener(busy);
}

/** A frame's reading as one of the three states. A missing or non-finite field is
 *  `absent` — the route sends an explicit null when the box exposes no gauge, and
 *  letting `undefined` through a bare null-check once rendered a literal NaN. */
function fromFrame(value: unknown): GpuBusy {
  return typeof value === "number" && Number.isFinite(value)
    ? { percent: value, state: "reading" }
    : { percent: null, state: "absent" };
}

function openStream(): void {
  if (source !== null) return;
  let stream: EventSource;
  try {
    stream = api.opsVitalsStream();
  } catch {
    // No EventSource in this runtime. This runs inside a React effect, so throwing
    // would unmount the whole top bar over a gauge — leave the reading unknown and
    // let everything else render.
    publish(UNKNOWN);
    return;
  }
  source = stream;
  stream.onmessage = (event: MessageEvent<string>) => {
    try {
      const frame = JSON.parse(event.data) as { gpu_busy_percent?: unknown };
      publish(fromFrame(frame.gpu_busy_percent));
    } catch {
      // A malformed frame must not kill the stream — the next tick is a second away.
    }
  };
  // EventSource reconnects on its own. Drop to `unknown`, NOT `absent`: the reading
  // has stopped being current, but nothing has said the gauge went away.
  stream.onerror = () => publish(UNKNOWN);
}

async function probe(): Promise<void> {
  try {
    const vitals = await api.opsVitals();
    if (listeners.size === 0 || !isForeground()) return; // gave up while awaiting
    access = "allowed";
    publish(fromFrame(vitals.gpu_busy_percent));
    openStream();
  } catch (error) {
    if (listeners.size === 0) return;
    // 401/403 is this principal's standing answer — stop asking. Anything else
    // (offline, server restarting) may pass later, so try again on a slow timer.
    if (isRejection(error)) {
      access = "denied";
      return;
    }
    reprobe = setTimeout(() => void probe(), REPROBE_MS);
  }
}

function start(): void {
  if (!isForeground() || access === "denied" || source !== null) return;
  if (access === "allowed") openStream();
  else void probe();
}

function stop(): void {
  source?.close();
  source = null;
  if (reprobe !== null) {
    clearTimeout(reprobe);
    reprobe = null;
  }
  publish(UNKNOWN);
}

function onVisibilityChange(): void {
  if (isForeground()) start();
  else stop();
}

/** Subscribe to the shared reading; returns an unsubscribe. The first subscriber
 *  starts the stream, the last one to leave stops it. */
export function subscribeGpuBusy(listener: Listener): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    document.addEventListener("visibilitychange", onVisibilityChange);
    start();
  } else {
    listener(published); // a late joiner gets the current reading, not a blank
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      stop();
    }
  };
}

/** The host's GPU busy percent and why it is or isn't there, refreshed once a second.
 *  Every caller shares one stream and therefore one answer — two top bars can never
 *  again disagree about whether the box has a GPU. */
export function useGpuBusy(): GpuBusy {
  const [busy, setBusy] = useState<GpuBusy>(() => published);
  useEffect(() => subscribeGpuBusy(setBusy), []);
  return busy;
}

/** True when the failure means "not for you" rather than "not right now". Anything
 *  that isn't an outright rejection is treated as retryable, so a transport quirk can
 *  never permanently blind the meter. */
function isRejection(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}
