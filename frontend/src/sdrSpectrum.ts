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
// **A row is drawn when its audio is HEARD, whenever there is audio to match it.**
// This said the opposite until the day it named arrived: a spectrum session is its own
// purpose on its own radio and makes no sound, so there was nothing to align with, and
// a delay added in advance would only have made the picture late.
//
// One radio now both demodulates and draws — `capture.ChannelSink` takes the sound and
// the picture from THE SAME SAMPLES — so every row of a listening session has an ear to
// be matched to. They were not matched: rows arrive over SSE at the live edge while
// playback sits behind it, so the strip showed a burst before the speaker played it and
// the waterfall drew a station the owner was not yet hearing. REPORTED as "the spectrum
// and the actual audio out of my speakers are not matched", and named the most important
// thing to get right. The tape drawn beside the strip is sampled off the audio element,
// so it was already on the ear's clock and the two disagreed on screen.
//
// **This delays the PICTURE, never the sound.** An earlier pass got the order wrong and
// built the hold while playback was still eight seconds behind, which made a correct
// mechanism look like an eight-second penalty. The audio latency is cut first
// (sdrAudio.ts `refused()` and `checkDrift()`); what is left is small, and this closes
// it exactly rather than approximately — `Frame.at` and the audio anchor are the same
// box clock (`time.time()` on the sidecar), so there is nothing to estimate.
//
// When nothing is playing — a spectrum-only session on the Radio tab, a paused radio —
// there is no ear, and rows go straight through exactly as they always did.

import { isSdrPlaying, sdrHeardAt } from "./sdrAudio";

/** One waterfall row, exactly as the sidecar framed it (deploy/sdr/listen.py Frame). */
/** One signal the sidecar found in a row: where it is, how strong, and how far it
 *  stands above the noise around it.
 *
 *  Found on the box rather than here, and that is the point: the agent's tools read the
 *  same frames, so "what is on the air" cannot have two answers depending on who asked.
 *  `overDb` travels with it because it is what decided it was a signal at all. */
export interface SpectrumPeak {
  /** The CHANNEL this signal is in, when the box could establish a grid — otherwise the
   *  same as `measuredHz`. This is what a pill is labelled with, and what makes the same
   *  station keep the same label row after row. */
  hz: number;
  /** Where its energy actually peaked. A hopped row sees each slice for milliseconds and
   *  a wideband-FM carrier sweeps its own deviation the whole time, so this moves from
   *  row to row even when the station does not. Kept so the label can be CHECKED against
   *  what was seen rather than believed. */
  measuredHz: number;
  db: number;
  overDb: number;
}

/** Which picture a row belongs to. One session now draws both off one capture — the
 *  band it is sitting in and the channel it is demodulating — so a reader has to be
 *  told rather than infer it. */
export type SpectrumView = "band" | "channel";

export interface SpectrumRow {
  /** Box clock when the row was measured. */
  at: number;
  startHz: number;
  stopHz: number;
  binHz: number;
  db: number[];
  /** Strongest first. Empty is a real answer — a quiet band — and is what a build
   *  without peak finding also sends, which is why nothing here treats it as unknown. */
  peaks: SpectrumPeak[];
  /** How wide the DEMODULATOR's passband is, when this row is a tuned CHANNEL rather
   *  than a band. Zero on a band row and on any row from a box that predates the
   *  demodulator, which is exactly the test for "is this a tuning view?" — one stream
   *  now carries both kinds, and each row says which it is rather than the reader
   *  having to know what the radio was asked for. */
  passbandHz: number;
  /** How far the passband's MIDDLE sits from the tuned frequency, in Hz. Zero on every
   *  symmetric mode — which is every mode but SSB, and on a row from a box that predates
   *  the field, so a strip that adds it draws exactly what it drew before everywhere
   *  else.
   *
   *  SSB is one-sided: `usb` hears +300..+3400 Hz and `lsb` hears -3400..-300, so
   *  shading `passbandHz` centred on the dial covered half the REJECTED sideband and
   *  left the top of the real one outside the box. Someone centring a signal in it put
   *  half the signal where nothing can hear it. */
  passbandCentreHz: number;
  /** How far apart the stations on this band are — 200 kHz on the FM dial, 25 kHz on
   *  the 2 m plan. Only the box knows the raster, and without it a viewer holding
   *  peaks across rows cannot tell ONE station whose loudest bin wanders from TWO that
   *  are genuinely apart. Zero when the band has no raster. */
  channelHz: number;
  /** The tuner gain this row was measured at, in dB, or null when the radio's own loop
   *  was running — and null too on a row from a box that predates the field (C22).
   *
   *  `db` is dBFS, and dBFS is only comparable against the SAME gain and the SAME
   *  `binHz`. A noise floor is power per bin, so a band at 250 Hz bins reads ~6 dB below
   *  the same band at 1 kHz; and under an automatic gain the absolute level means
   *  nothing between rows at all. A reader holding rows across time needs both to tell a
   *  floor that MOVED from a floor measured differently. */
  gainDb: number | null;
  /** Band or channel. Said by the box; inferred from `passbandHz` only for a row from
   *  an older sidecar, which is exactly how every reader used to guess. */
  view: SpectrumView;
}

export interface SpectrumState {
  /** True from the moment the owner opens the picture until it is closed. */
  on: boolean;
  /** The newest row of ANY view, or null while waiting for the first. */
  latest: SpectrumRow | null;
  /** The newest row of each picture, held apart. One stream carries both, so a viewer
   *  that took "the newest row" would draw a 2.4 MHz band into a 32 kHz tuning strip
   *  half the time — and the two are drawn side by side now, not in turn. */
  band: SpectrumRow | null;
  channel: SpectrumRow | null;
  /** How many rows have arrived on this stream. Lets a view say "warming up" without
   *  keeping a count of its own. */
  rows: number;
  /** Set when the box will not draw — a radio held by something else, most often.
   *  The sidecar's own sentence, which names the job holding it. */
  error: string | null;
}

type Listener = (state: SpectrumState, row: SpectrumRow | null) => void;

const IDLE: SpectrumState = {
  on: false,
  latest: null,
  band: null,
  channel: null,
  rows: 0,
  error: null,
};

/** What a surface wants drawn: which picture, off WHICH RADIO, with how much of what
 *  has already been drawn.
 *
 *  `serial` is not optional in spirit. The sidecar picks the session to stream from, and
 *  it prefers a spectrum one — so on a two-dongle box the tuner's own channel strip was
 *  attached to the other radio's band picture and waited for rows that session does not
 *  draw. A surface that knows which radio it is showing has to say so. */
export interface SpectrumWanted {
  view?: SpectrumView | "all";
  serial?: string | null;
  /** Rows already drawn to send before the live ones, so a picture that has been
   *  running for minutes comes back as a picture rather than as a blank box filling at
   *  the row rate. */
  backfill?: number;
}

function query(want: SpectrumWanted): string {
  const parts = [`view=${want.view ?? "all"}`];
  if (want.serial) parts.push(`serial=${encodeURIComponent(want.serial)}`);
  if (want.backfill && want.backfill > 1) parts.push(`backfill=${Math.round(want.backfill)}`);
  return parts.join("&");
}

let state: SpectrumState = IDLE;
let source: EventSource | null = null;
/** What the open stream was opened FOR, so a surface asking for something else reopens
 *  it instead of silently inheriting someone else's picture. */
let openKey: string | null = null;
const listeners = new Set<Listener>();

/** A row waiting for the ear, with the wall-clock moment it arrived — see `release`. */
interface Held {
  row: SpectrumRow;
  queuedAt: number;
}

// Rows arrive at the LIVE EDGE; the listener is behind it. These wait here until the
// audio they picture actually reaches the speaker.
let pending: Held[] = [];
let releaser: ReturnType<typeof setInterval> | null = null;

// Checked faster than rows arrive — the box caps itself at 10 fps — so a row comes out
// close to the moment it is due rather than at the mercy of the next arrival, which
// would also strand the last few rows of a stream that goes quiet.
const RELEASE_HZ = 20;
// A row held this long is drawn anyway. Both clocks are the box's own, so they should
// not drift — but the anchor is taken at the moment the stream is attached, and if it
// is wrong in the slow direction the picture would simply stop for ever. A late picture
// is a defect; a frozen one is a broken radio.
const MAX_HOLD_S = 20;
// The queue holds the playback delay's worth of rows. This is several times any delay
// the audio path should now produce, so it bounds a leak without biting in normal
// running.
const MAX_HELD = 400;

function publish(next: SpectrumState, row: SpectrumRow | null): void {
  state = next;
  for (const listener of listeners) listener(next, row);
}

function emit(row: SpectrumRow): void {
  publish(
    {
      on: true,
      latest: row,
      band: row.view === "band" ? row : state.band,
      channel: row.view === "channel" ? row : state.channel,
      rows: state.rows + 1,
      error: null,
    },
    // The row is handed to subscribers directly rather than read back off the state,
    // so a canvas draws exactly the rows that arrived — never one twice, never a
    // skipped one, whatever else re-publishes in between.
    row,
  );
}

function hold(row: SpectrumRow): void {
  pending.push({ row, queuedAt: Date.now() });
  if (pending.length > MAX_HELD) pending = pending.slice(-MAX_HELD);
  release();
}

/**
 * Draw a row when its audio is heard, not when it arrives.
 *
 * `isSdrPlaying()` as well as the anchor, because `sdrHeardAt()` goes on answering from
 * a PAUSED element — `currentTime` simply stops — and a radio paused while the owner
 * watches its picture would freeze the waterfall until they pressed play again. No ear
 * means no alignment to make, so rows go straight through.
 *
 * EVERY due row is released, in arrival order, not just the newest: each one is a line
 * of the waterfall, and dropping the ones that came due together would eat the history
 * the picture exists to show. That is the difference from `sdrCaptions`, where only the
 * newest due caption is worth showing.
 */
function release(): void {
  if (pending.length === 0) return;
  const heard = isSdrPlaying() ? sdrHeardAt() : null;
  if (heard === null) {
    const due = pending;
    pending = [];
    for (const entry of due) emit(entry.row);
    return;
  }
  const stale = Date.now() - MAX_HOLD_S * 1000;
  const still: Held[] = [];
  for (const entry of pending) {
    if (entry.row.at <= heard || entry.queuedAt <= stale) emit(entry.row);
    else still.push(entry);
  }
  pending = still;
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
  const passband =
    typeof payload.passband_hz === "number" && Number.isFinite(payload.passband_hz)
      ? Math.max(0, payload.passband_hz)
      : 0;
  return {
    at: typeof payload.at === "number" ? payload.at : 0,
    startHz: start,
    // DERIVED from the array, never copied from the payload's own stop: the renderer
    // places bin `i` at `startHz + i * binHz`, so the two must agree by construction.
    stopHz: start + values.length * bin,
    binHz: bin,
    db: values,
    peaks: parsePeaks(payload.peaks),
    passbandHz: passband,
    passbandCentreHz: centreOf(payload.passband_centre_hz),
    channelHz:
      typeof payload.channel_hz === "number" && Number.isFinite(payload.channel_hz)
        ? Math.max(0, payload.channel_hz)
        : 0,
    // NULL, not zero: 0 dB is a real gain this box has actually run at, so a falsy
    // default would be a reading rather than an absence — the C18 mistake in a
    // different file.
    gainDb:
      typeof payload.gain_db === "number" && Number.isFinite(payload.gain_db)
        ? payload.gain_db
        : null,
    view: parseView(payload.view, passband),
  };
}

/** How far the passband's middle sits off the dial, in Hz.
 *
 *  SIGNED, unlike every other number this file parses: which SIDE the passband sits on
 *  is the whole content of it, so the `Math.max(0, ...)` the neighbours use would erase
 *  `lsb`. Zero for anything unreadable, which is what a symmetric mode sends anyway. */
function centreOf(raw: unknown): number {
  return typeof raw === "number" && Number.isFinite(raw) ? raw : 0;
}

/** The row's own view, or the guess every reader used to make.
 *
 *  `passbandHz > 0` was the test for "is this a tuning view?" while exactly one kind of
 *  row could come from one session. It still answers correctly for a box that predates
 *  the field, which is the only case it is left for. */
function parseView(raw: unknown, passbandHz: number): SpectrumView {
  if (raw === "band" || raw === "channel") return raw;
  return passbandHz > 0 ? "channel" : "band";
}

/** The peaks off the wire, defensively: a row from an older box has none, and one bad
 *  entry must not cost the row. Anything that is not three finite numbers is dropped
 *  rather than drawn at a frequency it was never measured at. */
function parsePeaks(raw: unknown): SpectrumPeak[] {
  if (!Array.isArray(raw)) return [];
  const out: SpectrumPeak[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const { hz, measured_hz: measured, db, over_db: over } = entry as Record<string, unknown>;
    if (typeof hz !== "number" || !Number.isFinite(hz)) continue;
    if (typeof db !== "number" || !Number.isFinite(db)) continue;
    out.push({
      hz,
      // A box older than the snap sends no measurement, and this box sends none on a
      // band where it could not establish a grid. In both, the label IS the measurement.
      measuredHz: typeof measured === "number" && Number.isFinite(measured) ? measured : hz,
      db,
      overDb: typeof over === "number" && Number.isFinite(over) ? over : 0,
    });
  }
  return out;
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

/** Open the stream. Safe to call when already open.
 *
 *  `view` says WHICH picture to ask the box for, and it is not only a filter: a
 *  listening session transforms the whole 2.4 MHz band only while someone is
 *  subscribed to it (~11% of one core), so asking for a picture nothing draws is work
 *  the radio does for nobody. `all` is for a surface showing both at once. */
export function startSdrSpectrum(want: SpectrumWanted | SpectrumView | "all" = "all"): void {
  const asked: SpectrumWanted = typeof want === "string" ? { view: want } : want;
  const key = `${asked.view ?? "all"}|${asked.serial ?? ""}|${asked.backfill ?? 1}`;
  if (typeof EventSource === "undefined") return;
  if (source && key === openKey) {
    // The same picture of the same radio: nothing to reopen, and the rows already
    // drawn stay on whatever is drawing them.
    publish({ ...state, on: true }, null);
    return;
  }
  if (source) {
    // A DIFFERENT picture, which used to be ignored — "the first caller's view stands",
    // written when the tuner sheet and the Radio tab were different jobs on one dongle.
    // The omnibox sheet made them two tabs over two radios, so the second tab asked for
    // a picture it then never received: the tuning strip sat on "waiting for the radio"
    // under audio that was plainly playing.
    source.close();
    source = null;
    stopReleasing();
  }
  publish({ ...IDLE, on: true }, null);
  openKey = key;
  // One socket whichever it is: each row says which picture it belongs to, so a
  // surface that wants both needs no second stream.
  const stream = new EventSource(`/api/sdr/spectrum?${query(asked)}`);
  source = stream;
  pending = [];
  releaser ??= setInterval(release, 1000 / RELEASE_HZ);
  stream.onmessage = (event: MessageEvent<string>) => {
    const parsed = parseRow(event.data);
    if (!parsed) return; // a keepalive or a torn frame; the next row is a fresh chance
    if ("error" in parsed) {
      publish({ ...state, on: true, error: parsed.error }, null);
      return;
    }
    hold(parsed);
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
  openKey = null;
  stopReleasing();
  publish(IDLE, null);
}

function stopReleasing(): void {
  if (releaser !== null) clearInterval(releaser);
  releaser = null;
  // Dropped rather than flushed: these are rows the owner never saw, and a picture that
  // has been closed has nowhere to draw them.
  pending = [];
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
  openKey = null;
  stopReleasing();
  listeners.clear();
  state = IDLE;
}
