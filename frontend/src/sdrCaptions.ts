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

function publish(next: CaptionState): void {
  state = next;
  for (const listener of listeners) listener(next);
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
  const stream = new EventSource("/api/sdr/captions");
  source = stream;
  stream.onmessage = (event: MessageEvent<string>) => {
    const parsed = parse(event.data);
    if (!parsed) return;
    if ("error" in parsed) {
      publish({ ...state, on: true, error: parsed.error });
      return;
    }
    publish({ on: true, latest: parsed, error: null });
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
  listeners.clear();
  state = IDLE;
}
