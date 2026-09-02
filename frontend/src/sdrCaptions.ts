// Live captions for the radio, off by default.
//
// Whisper is not a streaming model, so the box cuts the live audio into segments and
// transcribes them one after another; this holds the newest line and who is listening.
// Words carry per-token confidence, which the caption tints on the SAME rose→amber→
// green scale as the transcript viewer (AudioTranscript.tsx exports `confidenceColor`).
//
// Off by default, and stopped the moment the sheet closes or the lease ends, because
// captions hold a whisper model resident on the box's GPU alongside the chat model.
// That cost is the whole reason this is a toggle rather than something always running.
//
// A module store rather than React context, matching sdrSession.ts and sdrAudio.ts.

import { sdrHeardAt } from "./sdrAudio";

export interface CaptionWord {
  text: string;
  confidence: number;
}

export interface Caption {
  /** Sidecar clock for the segment this came from; the caption's identity. */
  startedAt: number;
  text: string;
  words: CaptionWord[];
}

/** How long a segment covers, at most; a caption is shown from its START time plus a
 *  little, so a long segment does not sit invisible until its final word is heard. */
const SHOW_AFTER_S = 1.0;
/** If the audio timeline cannot be anchored, fall back to showing captions on arrival
 *  rather than never — early is worse than absent, but absent is worst. */
const RELEASE_TICK_MS = 250;

export type CaptionState = {
  /** True from the moment the owner turns CC on until it is turned off. */
  on: boolean;
  /** The newest caption, or null while listening for the first one. */
  latest: Caption | null;
  /** Set when the box cannot caption at all (no whisper gateway); clears on retry. */
  error: string | null;
};

type Listener = (state: CaptionState) => void;

const IDLE: CaptionState = { on: false, latest: null, error: null };

let state: CaptionState = IDLE;
let source: EventSource | null = null;
const listeners = new Set<Listener>();
// Captions arrive from the LIVE EDGE; the listener is seconds behind it. These wait
// here until the audio they describe actually reaches the speaker (see release()).
let pending: Caption[] = [];
let releaser: ReturnType<typeof setInterval> | null = null;

function publish(next: CaptionState): void {
  state = next;
  for (const listener of listeners) listener(next);
}

/**
 * Show captions when their audio is heard, not when they arrive.
 *
 * The box transcribes the live edge, but playback is a long way behind it — measured
 * against the real sidecar in Chromium, a constant ~8.3 s. Showing a caption on
 * arrival therefore puts the words on screen BEFORE the speech, by three to eight
 * seconds, which reads far worse than a caption that lags. `sdrHeardAt()` gives the
 * box-clock time of the audio currently coming out of the speaker, so each caption
 * simply waits for it.
 *
 * If the timeline cannot be anchored (no session clock yet), captions are released on
 * arrival rather than held forever: mistimed is bad, missing is worse.
 */
function release(): void {
  if (pending.length === 0) return;
  const heard = sdrHeardAt();
  if (heard === null) {
    const latest = pending[pending.length - 1] ?? null;
    pending = [];
    if (latest) publish({ ...state, latest });
    return;
  }
  let due: Caption | null = null;
  const held: Caption[] = [];
  for (const caption of pending) {
    if (caption.startedAt + SHOW_AFTER_S <= heard) due = caption;
    else held.push(caption);
  }
  pending = held;
  // Only the newest DUE caption is shown: if several came due at once the older ones
  // are already past being heard, and flashing through them helps nobody.
  if (due) publish({ ...state, latest: due });
}

function parse(raw: string): Caption | { error: string } | null {
  try {
    const payload = JSON.parse(raw) as Record<string, unknown>;
    if (typeof payload.error === "string") return { error: payload.error };
    if (typeof payload.text !== "string" || !payload.text) return null;
    const words = Array.isArray(payload.words)
      ? payload.words.flatMap((entry): CaptionWord[] => {
          if (typeof entry !== "object" || entry === null) return [];
          const word = entry as Record<string, unknown>;
          if (typeof word.text !== "string") return [];
          const confidence = typeof word.confidence === "number" ? word.confidence : 1;
          return [{ text: word.text, confidence }];
        })
      : [];
    return {
      startedAt: typeof payload.started_at === "number" ? payload.started_at : 0,
      text: payload.text,
      words,
    };
  } catch {
    return null; // a truncated frame; the next one is a fresh chance
  }
}

/** Turn captions on. Opens the stream; safe to call when already on. */
export function startSdrCaptions(): void {
  if (source || typeof EventSource === "undefined") {
    publish({ ...state, on: true });
    return;
  }
  publish({ on: true, latest: null, error: null });
  pending = [];
  if (releaser === null && typeof setInterval !== "undefined") {
    releaser = setInterval(release, RELEASE_TICK_MS);
  }
  const stream = new EventSource("/api/sdr/captions");
  source = stream;
  stream.onmessage = (event: MessageEvent<string>) => {
    const parsed = parse(event.data);
    if (!parsed) return;
    if ("error" in parsed) {
      publish({ ...state, on: true, error: parsed.error });
      return;
    }
    pending.push(parsed);
    // Bound the wait: a caption whose audio never arrives (a stalled element) must not
    // pile up. Twelve entries is far more than the ear can be behind.
    if (pending.length > 12) pending = pending.slice(-12);
    publish({ ...state, on: true, error: null });
  };
  stream.onerror = () => {
    // EventSource reconnects on its own, so a blip is not worth reporting. What is
    // worth reporting is a box that cannot caption at all — that arrives as a 503,
    // which closes the stream for good.
    if (stream.readyState === EventSource.CLOSED) {
      publish({ ...state, on: true, error: "Captions are not available on this box." });
    }
  };
}

/** Turn captions off and close the stream, freeing the model to be evicted. */
export function stopSdrCaptions(): void {
  source?.close();
  source = null;
  if (releaser !== null) {
    clearInterval(releaser);
    releaser = null;
  }
  pending = [];
  publish(IDLE);
}

/** The current reading, for a component mounting mid-stream. */
export function sdrCaptions(): CaptionState {
  return state;
}

/** Subscribe to caption changes; returns an unsubscribe. */
export function subscribeSdrCaptions(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Test seam: forget everything between cases. */
export function resetSdrCaptions(): void {
  source?.close();
  source = null;
  if (releaser !== null) {
    clearInterval(releaser);
    releaser = null;
  }
  pending = [];
  listeners.clear();
  state = IDLE;
}
