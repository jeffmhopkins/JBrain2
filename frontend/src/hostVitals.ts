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

import { currentTokenRate } from "./agent/tokenMeter";
import { ApiError, api } from "./api/client";
import { isForeground } from "./visibility";

/** How long to wait before re-probing after a failure that might pass — a server
 *  mid-restart, a dropped network. A rejection is never retried; see `access`. */
const REPROBE_MS = 30_000;

/** How long to wait before reopening a stream that died fatally. Shorter than a
 *  re-probe because the case this exists for is a deploy: the box is a few seconds from
 *  answering again, and the meter should not stay blank for half a minute after it. */
const REOPEN_MS = 5_000;

/** Everything that can mean "the app is back".
 *
 *  `visibilitychange` alone is not enough, and that is the bug this list fixes. In an iOS
 *  standalone PWA a background/foreground round trip frequently delivers only `pageshow`
 *  (the page is restored from the page cache) — no visibility event at all — and a resumed
 *  app often comes back on a different network, where `online` is the only signal. Missing
 *  the resume is not cosmetic: the socket is already dead by then, so the top bar sat on
 *  dashes until the whole app was restarted, while the detail screen — plain fetches — kept
 *  showing numbers. That is exactly the reported symptom.
 *
 *  These are deliberately additive to `visibilitychange`, not a replacement: several of them
 *  fire for the same resume, and `ensureLive` is idempotent precisely so that is harmless. */
const RESUME_EVENTS = ["pageshow", "focus", "online"] as const;

/** `EventSource.CLOSED`, as a literal. The constant is read off a stream instance that
 *  is a test double as often as a real EventSource, and the global does not exist in
 *  every runtime this module is imported into. */
const SOURCE_CLOSED = 2;

/** How long a stream may go silent before we stop believing it.
 *
 *  The route sends a frame every second whether or not the reading moved, precisely so
 *  silence is diagnostic. A stream can stop delivering while the socket stays OPEN — a
 *  proxy buffering `text/event-stream`, a half-open connection after a network change, a
 *  server that stopped writing — and none of those raise an error event, so the fatal-
 *  close recovery never fires. That is the state the top bar was found in: EventSource
 *  reporting a perfectly healthy connection with no data behind it, while the detail
 *  screen (plain fetches) kept showing numbers. Six missed frames is well clear of
 *  ordinary jitter. */
const SILENCE_MS = 6_000;

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

/** Seconds of history kept for the detail surface's 1/5/15-minute ranges. One sample a
 *  second, so 15 minutes is 900 — small enough to hold in memory, and the reason the
 *  ranges need no backend at all. The history is CLIENT-side, so it starts empty on a
 *  reload; surviving that would need a server-side ring. */
const HISTORY_SAMPLES = 900;

/** One second of the box, as the detail graph plots it. Either channel is null
 *  wherever there was no reading — a gap, never a zero, which would read as an idle
 *  GPU or as a turn generating nothing. */
export interface VitalsSample {
  at: number;
  gpu: number | null;
  tps: number | null;
}

const history: VitalsSample[] = [];

/** How far the box's clock is from this browser's, in ms, so a sample stamped ON THE BOX
 *  can be placed on the browser's timeline.
 *
 *  Samples are bucketed by the second they were READ, not the instant they arrived —
 *  that is what stopped the trace showing dropouts, because a few tens of milliseconds of
 *  arrival jitter was putting two readings in one second's slot and none in the next.
 *  Placing them needs the two clocks reconciled: a box running a second ahead would
 *  otherwise push every sample past "now" and the newest slot would never fill. */
let clockOffset = 0;

/** The lowest `arrival − stamp` seen in the current window, and how many frames into it we
 *  are. The MINIMUM is the estimate worth keeping: it is the offset plus the fastest trip
 *  the network managed, so it moves when the clocks move and not when the wifi hiccups. */
let offsetFloor = Number.POSITIVE_INFINITY;
let offsetFrames = 0;
let offsetKnown = false;

/** Frames per re-estimate of the offset — two minutes of stream. */
const OFFSET_WINDOW_FRAMES = 120;

/** How far the estimate must move before it is applied. Re-basing on every wobble would
 *  shift the whole grid by a few milliseconds every couple of minutes, which is its own
 *  way of dropping a sample across a slot boundary — the exact artifact this all exists to
 *  remove. A clock STEP (an ntp correction, a phone resuming on a new network) is far
 *  larger than that and does deserve to move the grid, once. */
const OFFSET_STEP_MS = 250;

/** Note what a frame says about the two clocks. */
function trackClockOffset(stampedAt: number, arrivedAt: number): void {
  offsetFloor = Math.min(offsetFloor, arrivedAt - stampedAt);
  offsetFrames += 1;
  if (offsetFrames < OFFSET_WINDOW_FRAMES && offsetKnown) return;
  if (!offsetKnown || Math.abs(offsetFloor - clockOffset) > OFFSET_STEP_MS) {
    clockOffset = offsetFloor;
    offsetKnown = true;
  }
  offsetFloor = Number.POSITIVE_INFINITY;
  offsetFrames = 0;
}

/** One `{at_ms, gpu}` the box recorded, as the stream sends them. */
interface BoxSample {
  at_ms: number;
  gpu: number | null;
}

/** The samples in a frame, or an empty list for anything that isn't one. A box that
 *  predates the stamped frame sends none, and the caller falls back to the arrival
 *  instant — a slightly jittery trace beats no trace. */
function frameSamples(raw: unknown): BoxSample[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((entry) => {
    const at = (entry as { at_ms?: unknown })?.at_ms;
    if (typeof at !== "number" || !Number.isFinite(at)) return [];
    const gpu = (entry as { gpu?: unknown })?.gpu;
    return [{ at_ms: at, gpu: typeof gpu === "number" && Number.isFinite(gpu) ? gpu : null }];
  });
}

type Listener = (busy: GpuBusy) => void;
type Access = "unknown" | "allowed" | "denied";

const listeners = new Set<Listener>();
let source: EventSource | null = null;
let access: Access = "unknown";
let published: GpuBusy = UNKNOWN;
let reprobe: ReturnType<typeof setTimeout> | null = null;
/** A probe is awaiting the server. Guards against a SECOND attempt starting while the
 *  first is still in flight — which is what happens when two readers mount together (the
 *  TopBar and the vitals card), since each one now asks for a health check. Without this
 *  they raced into two probes and, from there, two streams: the exact duplication the whole
 *  module-scoped-singleton design exists to prevent. */
let probing = false;
let silence: ReturnType<typeof setTimeout> | null = null;

/** What the stream has actually been doing, for the vitals screen's diagnostic row and
 *  the beacon behind it.
 *
 *  This exists because the top bar showed no reading while the detail screen — reading the
 *  server's ring over plain fetches — showed 97%, and no amount of server-side logging
 *  could distinguish "frames are not being sent", "frames are sent but never arrive", and
 *  "frames arrive but the meter drops them". Those need different fixes, and a connection
 *  the BROWSER declines to open leaves no trace on the box at all. So the browser reports
 *  what it saw. */
const diag = {
  opens: 0,
  frames: 0,
  errors: 0,
  reopensAfterClose: 0,
  reopensAfterSilence: 0,
  lastFrameAt: 0,
  lastErrorAt: 0,
  openFailed: "",
  /** When the current stream was opened. Lets "has delivered nothing yet" be told apart
   *  from "opened a minute ago and has delivered nothing", which is a dead stream. */
  openedAt: 0,
};

/** A snapshot of the stream's own health. `sinceLastFrameMs` is the number that matters:
 *  the route sends one a second, so anything above a few thousand means the meter is blind
 *  however healthy the socket claims to be. */
export function vitalsDiagnostics(): Record<string, unknown> {
  return {
    ...diag,
    access,
    listeners: listeners.size,
    hasSource: source !== null,
    // 0 CONNECTING, 1 OPEN, 2 CLOSED, -1 when there is no stream to ask.
    readyState: source?.readyState ?? -1,
    sinceLastFrameMs: diag.lastFrameAt === 0 ? -1 : Date.now() - diag.lastFrameAt,
    // Browser clock minus box clock, as estimated from the frames. A large figure here is
    // not a fault — it says the two clocks disagree, and that samples are being placed
    // with that disagreement taken out.
    clockOffsetMs: offsetKnown ? Math.round(clockOffset) : null,
    samples: history.length,
    published: published.state,
    foreground: isForeground(),
  };
}

function publish(busy: GpuBusy): void {
  published = busy;
  for (const listener of listeners) listener(busy);
}

/** Append what a frame carried to the shared ring.
 *
 *  Each sample is placed at the second the BOX read it, not the second the frame landed
 *  here. That distinction is the whole fix for the dropouts on the detail graph: the plot
 *  gives each whole second one slot, the box now emits exactly one sample per whole
 *  second, and arrival jitter used to be enough to drop two of them into one slot and
 *  leave the next empty — drawn as a gap in a history nothing had failed to record.
 *
 *  A frame carrying no samples is a box that doesn't stamp them; the arrival instant is
 *  then all there is, which is what this did for every frame before. */
function remember(busy: GpuBusy, raw: unknown, arrivedAt: number): void {
  const samples = frameSamples(raw);
  if (samples.length === 0) {
    append({ at: arrivedAt, gpu: busy.percent, tps: currentTokenRate() });
    return;
  }
  const newest = samples[samples.length - 1];
  if (newest !== undefined) trackClockOffset(newest.at_ms, arrivedAt);
  for (const sample of samples) {
    // The token rate rides the same sample as the GPU reading, on the stream's own 1 Hz
    // tick, so the detail graph plots both channels against ONE time axis rather than two
    // timers drifting against each other. It is a rolling browser-side measure with no
    // history to look up, so a sample the box is only now catching us up on carries the
    // rate as it is — true within a second or two, and closer than a hole would be.
    append({ at: sample.at_ms + clockOffset, gpu: sample.gpu, tps: currentTokenRate() });
  }
}

/** Add one sample, folding it into the head when it repeats a second already held — a
 *  reconnecting stream re-offers its newest sample, and a box that stamps nothing can land
 *  two frames inside one second. A second entry for one second buys nothing (the plot has
 *  one slot for it) and evicts a real second from the far end of the ring.
 *
 *  The higher reading wins the fold, as it does in the plot's own bucketing: this is a load
 *  gauge, and under-reporting a spike is the worse lie. */
function append(sample: VitalsSample): void {
  const head = history[history.length - 1];
  if (head !== undefined && Math.floor(head.at / 1000) === Math.floor(sample.at / 1000)) {
    if ((sample.gpu ?? -1) > (head.gpu ?? -1)) history[history.length - 1] = sample;
    return;
  }
  history.push(sample);
  if (history.length > HISTORY_SAMPLES) history.splice(0, history.length - HISTORY_SAMPLES);
}

/** Seed the ring with history recorded server-side, so the detail graph opens with a
 *  past instead of filling from empty after a reload.
 *
 *  Only samples OLDER than what this session already holds are taken: the live stream
 *  is the authority for anything it has seen, and its samples carry the token rate,
 *  which the server never sees (that is measured in the browser off the chat stream). */
export function seedVitalsHistory(seeds: { at_ms: number; gpu: number | null }[]): void {
  const now = Date.now();
  // Seeds are stamped by the SERVER's clock and bucketed against the browser's, so they
  // go through the same offset the live frames do — otherwise the two halves of one trace
  // sit on two grids, and the seam between them is a gap. A box running ahead would place
  // a several-seconds-old peak in the newest column and draw it as the current reading,
  // which the `<= now` filter is here to refuse.
  const usable = seeds
    .map((s) => ({ at: s.at_ms + clockOffset, gpu: s.gpu }))
    .filter((s) => s.at <= now);
  if (usable.length === 0) return;

  // Merge by second rather than only prepending what predates the ring. Backgrounding
  // closes the stream but keeps the samples already held, so the gap the server DID
  // record sits in the MIDDLE of this session's history — the exact case the ring was
  // added for, and one an "older than the earliest" rule filters out entirely.
  const held = new Map(history.map((sample) => [Math.floor(sample.at / 1000), sample]));
  for (const seed of usable) {
    const second = Math.floor(seed.at / 1000);
    // A sample this session saw wins: it carries the token rate, which the server
    // never sees, and its timestamp is on the clock the plot buckets against.
    if (!held.has(second)) held.set(second, { at: seed.at, gpu: seed.gpu, tps: null });
  }
  const merged = [...held.values()].sort((a, b) => a.at - b.at);
  history.length = 0;
  history.push(...merged.slice(-HISTORY_SAMPLES));
}

/** The samples inside the trailing `seconds`, oldest first. */
export function vitalsHistory(seconds: number): VitalsSample[] {
  const cutoff = Date.now() - seconds * 1000;
  return history.filter((sample) => sample.at >= cutoff);
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
  } catch (error) {
    // No EventSource in this runtime. This runs inside a React effect, so throwing
    // would unmount the whole top bar over a gauge — leave the reading unknown and
    // let everything else render.
    diag.openFailed = String(error).slice(0, 200);
    publish(UNKNOWN);
    return;
  }
  source = stream;
  diag.opens += 1;
  diag.openedAt = Date.now();
  armSilenceWatchdog(stream);
  stream.onmessage = (event: MessageEvent<string>) => {
    armSilenceWatchdog(stream); // a frame arrived: the stream is alive, restart the clock
    diag.frames += 1;
    diag.lastFrameAt = Date.now();
    try {
      const frame = JSON.parse(event.data) as {
        gpu_busy_percent?: unknown;
        samples?: unknown;
      };
      const busy = fromFrame(frame.gpu_busy_percent);
      remember(busy, frame.samples, Date.now());
      publish(busy);
    } catch {
      // A malformed frame must not kill the stream — the next tick is a second away.
    }
  };
  // Drop to `unknown`, NOT `absent`: the reading has stopped being current, but nothing
  // has said the gauge went away.
  stream.onerror = () => {
    diag.errors += 1;
    diag.lastErrorAt = Date.now();
    publish(UNKNOWN);
    // EventSource retries a DROPPED connection by itself, but a FATAL one — the box
    // answering 502 mid-deploy, a proxy closing with the wrong content type — leaves it
    // CLOSED, where it never retries again. `source` stayed set through that, so
    // `start()` short-circuited on `source !== null` and the meter was dead for the rest
    // of the session: a deploy blanked the top bar until the app was reloaded, while the
    // detail screen kept showing numbers because it reads the server's ring over plain
    // fetches. Treat CLOSED as "reopen it myself".
    if (stream.readyState === SOURCE_CLOSED) {
      diag.reopensAfterClose += 1;
      reopenLater(stream);
    }
  };
}

/** (Re)start the silence timer for a stream. A stream that goes quiet is replaced, not
 *  waited on: the reading is already stale by then, and nothing else will notice. */
function armSilenceWatchdog(stream: EventSource): void {
  if (silence !== null) clearTimeout(silence);
  silence = setTimeout(() => {
    silence = null;
    if (source !== stream) return;
    publish(UNKNOWN); // stale is not a reading — say so before trying again
    diag.reopensAfterSilence += 1;
    reopenLater(stream);
  }, SILENCE_MS);
}

/** Tear a stream down and try again shortly. Guarded against reopening a stream that has
 *  already been replaced or stopped, so a late error from a torn-down EventSource cannot
 *  resurrect one nobody is listening to.
 *
 *  Callers decide WHEN: the error path only acts on a CLOSED socket, because EventSource
 *  handles a dropped one itself. The silence path acts on a socket still claiming to be
 *  OPEN — which is exactly the failure a readyState check would wave through. */
function reopenLater(stream: EventSource): void {
  if (source !== stream) return;
  stream.close();
  source = null;
  if (reprobe !== null) return; // a genuine retry is already pending
  armRetry(start, REOPEN_MS);
}

async function probe(): Promise<void> {
  probing = true;
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
    armRetry(() => void probe(), REPROBE_MS);
  } finally {
    probing = false;
  }
}

/** Schedule the next attempt, clearing the handle when it fires.
 *
 *  Every retry goes through here because the handle is not just a timer — `reopenLater`
 *  reads it as "an attempt is already scheduled, don't add another". A callback that
 *  forgot to clear it therefore did not merely leak a handle: it left `reprobe` non-null
 *  forever, and from that moment the silence watchdog could tear a dead stream down but
 *  never reopen it. The meter stayed blind for the rest of the session, with no reconnect
 *  attempts reaching the box at all — which is precisely what the api log showed while the
 *  server was serving 97% perfectly well. One probe failure was enough to arm it, and a
 *  probe fails on every deploy, when /ops/vitals is briefly gone. */
function armRetry(attempt: () => void, delay: number): void {
  if (reprobe !== null) clearTimeout(reprobe);
  reprobe = setTimeout(() => {
    reprobe = null;
    attempt();
  }, delay);
}

/** Is the stream demonstrably DELIVERING? Deliberately not "does the socket claim to be
 *  open" — that is the question that has been wrong every single time this meter has gone
 *  blind. A suspended app's EventSource comes back reporting OPEN with nothing behind it. */
function streamIsLive(): boolean {
  if (source === null) return false;
  const last = diag.lastFrameAt === 0 ? diag.openedAt : diag.lastFrameAt;
  return Date.now() - last < SILENCE_MS;
}

/** Make sure a reading is actually flowing, recycling the stream if it isn't.
 *
 *  This is the recovery path that does not depend on a timer, which matters because the
 *  silence watchdog is a `setTimeout` and a backgrounded app's timers are frozen with it —
 *  so the one mechanism meant to catch a dead-but-OPEN socket is asleep at precisely the
 *  moment the socket dies. Safe to call as often as we like: it returns immediately when
 *  frames are arriving. */
function ensureLive(): void {
  if (!isForeground() || access === "denied") return;
  if (streamIsLive()) return;
  if (source !== null) {
    source.close();
    source = null;
  }
  // Drop any pending slow retry: we know NOW that the stream is dead and the app is in
  // front of the owner, so waiting out a 30s re-probe timer is just dashes on screen.
  if (reprobe !== null) {
    clearTimeout(reprobe);
    reprobe = null;
  }
  start();
}

function start(): void {
  if (!isForeground() || access === "denied" || source !== null || probing) return;
  if (access === "allowed") openStream();
  else void probe();
}

function stop(): void {
  source?.close();
  source = null;
  if (silence !== null) {
    clearTimeout(silence);
    silence = null;
  }
  if (reprobe !== null) {
    clearTimeout(reprobe);
    reprobe = null;
  }
  publish(UNKNOWN);
}

function onVisibilityChange(): void {
  if (isForeground()) ensureLive();
  else stop();
}

/** Subscribe to the shared reading; returns an unsubscribe. The first subscriber
 *  starts the stream, the last one to leave stops it. */
export function subscribeGpuBusy(listener: Listener): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    document.addEventListener("visibilitychange", onVisibilityChange);
    for (const event of RESUME_EVENTS) window.addEventListener(event, ensureLive);
    start();
  } else {
    listener(published); // a late joiner gets the current reading, not a blank
    // …and then a health check, because a NEW READER APPEARING is itself evidence the app
    // is in use. Opening the vitals card used to show live numbers (it polls) while the top
    // bar behind it stayed on dashes, since nothing about mounting a second reader ever
    // questioned the shared stream. Mounting one is now a reason to re-check it.
    ensureLive();
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      for (const event of RESUME_EVENTS) window.removeEventListener(event, ensureLive);
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
