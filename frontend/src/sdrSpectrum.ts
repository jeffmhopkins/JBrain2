// The live spectrum: rows of dB off the box, held for one canvas to draw.
//
// A module store rather than React state, and that is the whole design. A row is
// hundreds of numbers arriving up to ten times a second; putting it through `useState`
// would re-render a tree for a picture that is drawn imperatively anyway. Components
// subscribe and draw; nothing here renders.
//
// Shaped after sdrCaptions.ts — an EventSource, a refcount, and a reset seam — because
// this is the same kind of thing: a stream the owner turns on, off by default, holding
// a radio while it runs.
//
// **No delay is applied, and that is deliberate.** Captions are held back ~8.3 s to
// match the ear (sdrCaptions.ts), and an early sketch of this said the waterfall should
// be too. It should not: a spectrum session is its own purpose on its own radio and
// produces NO audio, so there is nothing to align with. The day one radio both
// demodulates and draws — the single-hop `rtl_sdr` + FFT tier bands.py calls `fast` —
// alignment becomes a real question. It is not one now, and a delay added in advance
// would only make the picture late.

/** One waterfall row, exactly as the sidecar framed it (deploy/sdr/listen.py Frame). */
export interface SpectrumRow {
  /** Box clock when the row was measured. */
  at: number;
  startHz: number;
  stopHz: number;
  binHz: number;
  db: number[];
}

export interface SpectrumState {
  /** True from the moment the owner opens the picture until it is closed. */
  on: boolean;
  /** The newest row, or null while waiting for the first. */
  latest: SpectrumRow | null;
  /** How many rows have arrived on this stream. Lets a view say "warming up" without
   *  keeping a count of its own. */
  rows: number;
  /** Set when the box will not draw — a radio held by something else, most often.
   *  The sidecar's own sentence, which names the job holding it. */
  error: string | null;
}

type Listener = (state: SpectrumState, row: SpectrumRow | null) => void;

const IDLE: SpectrumState = { on: false, latest: null, rows: 0, error: null };

let state: SpectrumState = IDLE;
let source: EventSource | null = null;
const listeners = new Set<Listener>();

function publish(next: SpectrumState, row: SpectrumRow | null): void {
  state = next;
  for (const listener of listeners) listener(next, row);
}

/** One SSE payload as a row, or null for a keepalive, an error, or a torn frame.
 *
 *  Tolerant in the same way the sidecar's own parser is: this is text a radio wrote
 *  while it was still writing, and one unreadable row must not end a live picture. */
export function parseRow(raw: string): SpectrumRow | { error: string } | null {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (typeof payload.error === "string") return { error: payload.error };
  const { start_hz: start, bin_hz: bin, db } = payload;
  if (typeof start !== "number" || typeof bin !== "number" || bin <= 0) return null;
  if (!Array.isArray(db) || db.length === 0) return null;
  const values = db.map((v) => (typeof v === "number" && Number.isFinite(v) ? v : Number.NaN));
  return {
    at: typeof payload.at === "number" ? payload.at : 0,
    startHz: start,
    // DERIVED from the array, never copied from the payload's own stop: the renderer
    // places bin `i` at `startHz + i * binHz`, so the two must agree by construction.
    stopHz: start + values.length * bin,
    binHz: bin,
    db: values,
  };
}

/** Whether two rows describe the same band at the same resolution.
 *
 *  A retune arrives with no message of its own — each row simply starts describing
 *  somewhere else — so this is how a viewer notices. It is also when the colour scale
 *  has to be re-taken: a new band has a new noise floor, and holding the old one paints
 *  the whole picture one flat colour.
 *
 *  **The axis is `startHz + i * binHz`, so those two are the whole question and the
 *  ROW LENGTH is not part of it.** Length used to be compared exactly, and that made a
 *  dropped block indistinguishable from a retune: `Stitch._flush` emits a deliberately
 *  short frame when one hop of a section goes missing, so on an eight-hop band a single
 *  lost block blanked the history and threw away a frozen colour scale that had taken
 *  eighty rows to earn. It is not a different band — every column that did arrive is at
 *  the same frequency it was before, which is exactly why `paint` already draws a short
 *  row and leaves the missing columns transparent rather than claiming a measurement.
 *
 *  **Compared to within half a bin, not exactly.** Exact equality was safe while
 *  `rtl_power` printed the edges it was asked for; the I/Q engine reads the ACHIEVED
 *  sample rate back off the hardware, which is not the requested one, so `bin_hz` and a
 *  `start_hz` derived from it can flap by a hertz between frames. Under exact equality
 *  that flap is a retune ten times a second. Half a bin is the honest threshold because
 *  it is the resolution the picture HAS — a shift the renderer cannot place in a
 *  different column is not a shift anyone can see. The bin width is held to the same
 *  threshold ACROSS THE ROW, not per bin: a width that really changed pulls the far end
 *  of the axis a whole column out of place, which is the top-edge check as it was.
 *
 *  What this cannot tell apart is a retune that keeps the same low edge AND the same
 *  bin width and only narrows the span — and that costs a stale colour window, where
 *  the mistake it replaces cost the picture on every dropped block. */
export function sameBand(a: SpectrumRow | null, b: SpectrumRow | null): boolean {
  if (!a || !b) return false;
  const tolerance = Math.min(a.binHz, b.binHz) / 2;
  if (Math.abs(a.startHz - b.startHz) > tolerance) return false;
  // Over the bins the two rows SHARE, because that is as far as either can disagree.
  return Math.abs(a.binHz - b.binHz) * Math.min(a.db.length, b.db.length) <= tolerance;
}

/** Open the stream. Safe to call when already open. */
export function startSdrSpectrum(): void {
  if (source || typeof EventSource === "undefined") {
    publish({ ...state, on: true }, null);
    return;
  }
  publish({ on: true, latest: null, rows: 0, error: null }, null);
  const stream = new EventSource("/api/sdr/spectrum");
  source = stream;
  stream.onmessage = (event: MessageEvent<string>) => {
    const parsed = parseRow(event.data);
    if (!parsed) return; // a keepalive or a torn frame; the next row is a fresh chance
    if ("error" in parsed) {
      publish({ ...state, on: true, error: parsed.error }, null);
      return;
    }
    publish(
      { on: true, latest: parsed, rows: state.rows + 1, error: null },
      // The row is handed to subscribers directly rather than read back off the state,
      // so a canvas draws exactly the rows that arrived — never one twice, never a
      // skipped one, whatever else re-publishes in between.
      parsed,
    );
  };
  stream.onerror = () => {
    // EventSource reconnects on its own, so a blip is not worth saying anything about.
    // A CLOSED stream is not a blip: the route answered in a way it will not retry.
    if (stream.readyState === EventSource.CLOSED) {
      publish({ ...state, on: true, error: "The box stopped sending the spectrum." }, null);
    }
  };
}

/** Close the stream. Does NOT release the radio — the session outlives the view, the
 *  same way audio outlives the tuner sheet, so re-opening the picture is instant. */
export function stopSdrSpectrum(): void {
  source?.close();
  source = null;
  publish(IDLE, null);
}

/** The current reading, for a component mounting mid-stream. */
export function sdrSpectrum(): SpectrumState {
  return state;
}

/** Subscribe to rows; returns an unsubscribe. */
export function subscribeSdrSpectrum(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Test seam: forget everything between cases. */
export function resetSdrSpectrum(): void {
  source?.close();
  source = null;
  listeners.clear();
  state = IDLE;
}
