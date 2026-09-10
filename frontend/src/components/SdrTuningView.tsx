// The tuning view: a short spectrum of the channel being listened to.
//
// Binding spec: docs/mocks/sdr-tuning-view/. The owner's ask was "a narrow spectrum
// view that's signal bw*2 but centered — would help with tuning", and the span rule
// the mock settled on is twice the DEMODULATOR'S PASSBAND, so the shaded band is a
// fixed fraction of the picture on every band. That makes "am I centred?" a shape
// question rather than a number one.
//
// **The picture and the sound are the same samples.** The sidecar demodulates the I/Q
// it is already capturing (`deploy/sdr/demod.py`) and transforms the decimated stream
// for this row, so the strip cannot disagree with what is coming out of the speaker —
// there is no second measurement for it to disagree with. It is also sharper than a
// zoom into the wideband waterfall: 512 bins over a 48 kHz IF is 94 Hz, against the
// 600 Hz a 4000-bin transform of the whole 2.4 MHz capture gives.
//
// Drawn on a canvas rather than as SVG because it repaints ten times a second and the
// trace is a few hundred points: as SVG that is a few hundred DOM nodes replaced per
// frame, which is the one shape of this that a phone notices. The text is DOM, for the
// reason the waterfall's markers are — type through a canvas transform is type drawn
// badly.

import { useEffect, useRef, useState } from "react";
import {
  bandwidthEdges,
  bandwidthSpoken,
  pendingPassband,
  snapBandwidth,
  stepBandwidth,
  widthFromOffset,
} from "../sdrBandwidth";
import { type SpectrumRow, sdrSpectrum, subscribeSdrSpectrum } from "../sdrSpectrum";
import { type Tuning, offsetLabel, passbandEdges, spillLabel, tuningOf } from "../sdrTuning";
import { type Scale as FallScale, paint as fallPaint, reduce, shadeRow } from "../sdrWaterfall";

/** CSS pixels of chart height, in BOTH modes. The owner's ask for the waterfall was
 *  "same footprint, same bandwidth", so the two are the same picture of the same
 *  channel drawn two ways and nothing below either of them moves when you switch. */
const CHART_H = 78;

/** How far the colour window may drift before the waterfall is repainted from its
 *  numbers rather than scrolled. Scrolling is what makes it cheap — one row of colour
 *  a frame instead of the whole picture — but the pixels already on the canvas were
 *  coloured against the OLD window, so a window that has really moved leaves the
 *  history saying something it did not measure. A decibel is under what the eye can
 *  see in this palette and rare enough that the repaint costs nothing in practice. */
const REPAINT_DB = 1;

/** How much of the row's own dynamic range the picture spans, and the floor it never
 *  collapses below. A channel with nothing in it has almost no spread, and a scale
 *  stretched across that turns rounding into a light show — the same reason
 *  `sdrWaterfall.calibrate` has a `MIN_SPAN_DB`. */
const MIN_SPAN_DB = 24;
const HEADROOM_DB = 4;

/** How many rows the READING is averaged over before it is called an offset.
 *
 *  The picture is drawn from the newest row alone — that is what makes it live — but
 *  the sentence under it is not, and this is why. An FM signal's instantaneous
 *  spectrum is asymmetric because the carrier is swinging, so a single 100 ms frame
 *  puts the signal's apparent centre somewhere it is not: measured on air, a
 *  correctly-tuned 104.1 read 39 kHz off. Averaged over eight frames the swing cancels
 *  and what is left is where the carrier actually sits.
 *
 *  Eight is 0.8 s, which is fast enough that turning the dial still feels immediate. */
const READING_ROWS = 8;

/** How fast the vertical scale follows the row. Per frame, at ~10 fps. A scale that
 *  snapped would make the trace jump every time a transmission started; one that
 *  never moved would push a strong signal off the top. */
const SCALE_EASE = 0.2;

interface Scale {
  lowDb: number;
  highDb: number;
}

function token(el: Element, name: string, fallback: string): string {
  const value = getComputedStyle(el).getPropertyValue(name).trim();
  return value || fallback;
}

/** Paint one row. Everything that is not text: the passband, the trace, the centre
 *  line, and the caret on a signal that is off it. */
function paint(
  canvas: HTMLCanvasElement,
  row: SpectrumRow,
  tuning: Tuning | null,
  scale: Scale,
): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  const steel = token(canvas, "--steel", "#7fa7c9");
  const tint = token(canvas, "--steel-tint", "rgba(127,167,201,0.13)");
  const amber = token(canvas, "--amber", "#c9a36a");
  const text = token(canvas, "--text", "#e6e7e9");

  ctx.clearRect(0, 0, w, h);
  const span = Math.max(row.stopHz - row.startHz, 1);
  const xOf = (hz: number) => ((hz - row.startHz) / span) * w;
  const yOf = (db: number) => {
    const t = (db - scale.lowDb) / Math.max(scale.highDb - scale.lowDb, 1e-6);
    return h - Math.min(1, Math.max(0, t)) * h;
  };

  // The passband. The ROW is centred on the tuned frequency — which is the row's own
  // middle — but the passband need not be: SSB is one-sided, so `passbandCentreHz`
  // offsets the shading to where the demodulator actually listens (C14). Zero on every
  // symmetric mode, so this is the old arithmetic everywhere else.
  const centre = row.startHz + span / 2;
  const band = passbandEdges(row);
  const left = xOf(centre + band.lowHz);
  const right = xOf(centre + band.highHz);
  ctx.fillStyle = tint;
  ctx.fillRect(left, 0, right - left, h);
  ctx.strokeStyle = steel;
  ctx.globalAlpha = 0.6;
  ctx.lineWidth = 1;
  for (const x of [left, right]) {
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, 0);
    ctx.lineTo(Math.round(x) + 0.5, h);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // The trace. Built once and used for both the fill and the stroke, so the outline
  // and the area under it can never describe different numbers.
  const line = new Path2D();
  const step = w / Math.max(row.db.length - 1, 1);
  let started = false;
  for (let i = 0; i < row.db.length; i += 1) {
    const value = row.db[i] as number;
    if (!Number.isFinite(value)) continue;
    const x = i * step;
    const y = yOf(value);
    if (started) line.lineTo(x, y);
    else {
      line.moveTo(x, y);
      started = true;
    }
  }
  if (started) {
    const area = new Path2D(line);
    area.lineTo(w, h);
    area.lineTo(0, h);
    area.closePath();
    ctx.globalAlpha = 0.22;
    ctx.fillStyle = steel;
    ctx.fill(area);
    ctx.globalAlpha = 1;
    ctx.strokeStyle = steel;
    ctx.lineWidth = 1.25;
    ctx.lineJoin = "round";
    ctx.stroke(line);
  }

  // The centre: where the radio is actually tuned.
  ctx.strokeStyle = text;
  ctx.globalAlpha = 0.8;
  ctx.beginPath();
  ctx.moveTo(Math.round(w / 2) + 0.5, 0);
  ctx.lineTo(Math.round(w / 2) + 0.5, h);
  ctx.stroke();
  ctx.globalAlpha = 1;

  // The caret, only when there is something to point at that is not already centred.
  if (tuning && !tuning.centred) {
    const x = xOf(centre + tuning.offsetHz);
    const y = yOf(tuning.peakDb);
    ctx.fillStyle = amber;
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x - 4, y - 7);
    ctx.lineTo(x + 4, y - 7);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = amber;
    ctx.globalAlpha = 0.7;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, y - 9);
    ctx.lineTo(Math.round(x) + 0.5, 0);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
  }
}

/** The channel over time, in the same frame the trace uses.
 *
 *  One row of history per DEVICE pixel row and one column per BIN: the vertical axis
 *  is never resampled, which is the twinkle fix the wideband waterfall needed, applied
 *  here before it can happen. The horizontal axis is UPSAMPLED — a few hundred bins
 *  across a thousand device columns — and that is safe where the vertical was not,
 *  because the factor is fixed and the data does not move through it.
 *
 *  Scrolled rather than repainted: only the newest row is coloured each frame. The
 *  history is kept as NUMBERS as well, because a scrolled canvas cannot be recoloured
 *  when the window moves, and `REPAINT_DB` is when that is cashed in. */
function fall(
  canvas: HTMLCanvasElement,
  off: HTMLCanvasElement,
  history: readonly SpectrumRow[],
  scale: FallScale,
  painted: { scale: FallScale; bins: number; rows: number } | null,
): { scale: FallScale; bins: number; rows: number } {
  const newest = history[0];
  if (!newest) return painted ?? { scale, bins: 0, rows: 0 };
  const bins = newest.db.length;
  const rows = canvas.height;
  const octx = off.getContext("2d");
  const ctx = canvas.getContext("2d");
  if (!octx || !ctx) return painted ?? { scale, bins, rows };
  if (off.width !== bins) off.width = bins;
  if (off.height !== rows) off.height = rows;

  const stale =
    painted === null ||
    painted.bins !== bins ||
    painted.rows !== rows ||
    Math.abs(painted.scale.lowDb - scale.lowDb) > REPAINT_DB ||
    Math.abs(painted.scale.highDb - scale.highDb) > REPAINT_DB;
  if (stale) {
    // Everything the strip remembers, recoloured against the window it is drawn with
    // now. `paint` fills from the BOTTOM, so the newest row sits against the frequency
    // axis it is measured on — the same convention the wideband waterfall settled on.
    const buffer = fallPaint(history, bins, rows, scale);
    octx.putImageData(new ImageData(buffer, bins, rows), 0, 0);
  } else {
    // Up by one, newest at the bottom. Drawing a canvas onto itself is defined to
    // snapshot the source first, so this is a scroll and not a smear.
    octx.drawImage(off, 0, -1);
    octx.putImageData(
      new ImageData(shadeRow(reduce(newest.db, bins), bins, scale), bins, 1),
      0,
      rows - 1,
    );
  }
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // Nearest-neighbour on the way up: a bin becomes a block of identical columns rather
  // than a gradient between two measurements that were never taken.
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0, canvas.width, canvas.height);
  return { scale, bins, rows };
}

/** The tuning references, drawn over whichever picture is underneath. Both modes need
 *  them and neither owns them: the passband is what the demodulator hears and the
 *  centre is where the radio is pointed, and those are true of a waterfall too. */
function guides(
  canvas: HTMLCanvasElement,
  row: SpectrumRow,
  ratio: number,
  override: { lowHz: number; highHz: number } | null = null,
): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  const span = Math.max(row.stopHz - row.startHz, 1);
  const centre = row.startHz + span / 2;
  const xOf = (hz: number) => ((hz - row.startHz) / span) * w;
  ctx.lineWidth = ratio;
  ctx.strokeStyle = token(canvas, "--steel", "#7fa7c9");
  ctx.globalAlpha = 0.55;
  // The row's own passband, UNLESS a width is in flight. A retune takes ~100 ms and the
  // row keeps arriving with the old passband throughout, so guides drawn from it sit at
  // the old edges while the handles the finger moved sit at the new ones — the picture
  // contradicting itself at exactly the moment the owner is watching it (MEASURED on the
  // box: mode button reading 8k, shading still 16k).
  const band = override ?? passbandEdges(row);
  for (const hz of [centre + band.lowHz, centre + band.highHz]) {
    ctx.beginPath();
    ctx.moveTo(Math.round(xOf(hz)) + 0.5, 0);
    ctx.lineTo(Math.round(xOf(hz)) + 0.5, h);
    ctx.stroke();
  }
  ctx.strokeStyle = token(canvas, "--text", "#e6e7e9");
  ctx.globalAlpha = 0.7;
  ctx.beginPath();
  ctx.moveTo(Math.round(w / 2) + 0.5, 0);
  ctx.lineTo(Math.round(w / 2) + 0.5, h);
  ctx.stroke();
  ctx.globalAlpha = 1;
}

/** Bin `i` averaged across the rows held for the reading. In the linear domain, not
 *  in dB: averaging decibels is averaging logarithms, which under-weights exactly the
 *  loud frames the signal is in and biases the answer toward the quiet ones. */
function average(rows: readonly SpectrumRow[], bin: number): number {
  let total = 0;
  let seen = 0;
  for (const row of rows) {
    const value = row.db[bin];
    if (typeof value === "number" && Number.isFinite(value)) {
      total += 10 ** (value / 10);
      seen += 1;
    }
  }
  return seen === 0 ? Number.NaN : 10 * Math.log10(total / seen);
}

function kHz(hz: number): string {
  const value = hz / 1000;
  return Number.isInteger(value) ? `${value}` : value.toFixed(1);
}

export function SdrTuningView({
  frequencyHz,
  onTune,
  demodMode,
  bandwidthHz = 0,
  minHz = 0,
  maxHz = 0,
  viewSpanHz = 0,
  viewSpans = [],
  onViewSpan,
  onBandwidth,
}: {
  /** What the radio is tuned to. Passed rather than read off the row: the row is
   *  cropped to whole bins, so its midpoint can sit half a bin from the frequency. */
  frequencyHz: number;
  /** Retune to where the signal actually is. Absent when the surface cannot retune. */
  onTune?: (hz: number) => void;
  /** The DEMODULATION mode — `am`, `usb`, … — which decides where the passband sits.
   *  Named apart from this component's own `mode`, which is trace-or-waterfall. */
  demodMode?: string;
  /** The filter width in force, and every width this mode offers. Both from the
   *  session, so the ladder is the box's rather than a copy that can go stale. */
  bandwidthHz?: number;
  minHz?: number;
  maxHz?: number;
  /** How wide the picture is drawn, and the widths this mode offers. Changing it is a
   *  CROP on the box — no rebuild, no click in the audio — so unlike the bandwidth it
   *  can be tapped freely while listening. */
  viewSpanHz?: number;
  viewSpans?: number[];
  onViewSpan?: ((hz: number) => void) | undefined;
  /** Set a new width. Absent where the filter cannot be changed — a spectrum stare, or
   *  wide FM, where narrowing clips the deviation and distorts rather than cleans. */
  onBandwidth?: ((hz: number) => void) | undefined;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const scaleRef = useRef<Scale>({ lowDb: -90, highDb: -20 });
  // The row is held in a ref and painted imperatively — at 10 fps, putting a few
  // hundred numbers through React state would re-render the whole surface for a
  // picture that is drawn on a canvas anyway. Only the SENTENCE is state, and it
  // changes when the reading does rather than when the row does.
  const [tuning, setTuning] = useState<Tuning | null>(null);
  const [row, setRow] = useState<SpectrumRow | null>(null);
  const recentRef = useRef<SpectrumRow[]>([]);
  // TRACE is what the strip was built as — where the signal is right now. FALL is the
  // same channel over TIME, which is the question a trace cannot answer: a repeater
  // that keyed up four seconds ago left nothing on an instantaneous picture.
  const [mode, setMode] = useState<"trace" | "fall">("trace");
  // The width the finger is currently over, or null when nothing is being dragged. Held
  // separately from `bandwidthHz` so the shaded box follows the finger immediately and
  // the committed value stays whatever the box last confirmed — a drag that the sidecar
  // refuses therefore snaps back rather than leaving the picture lying.
  const [dragHz, setDragHz] = useState<number | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);
  // The passband the UI is showing, which is not the row's until the retune lands. A ref
  // as well as state because the 10 fps repaint is imperative and must not re-subscribe
  // to the stream every time a finger moves.
  const pendingRef = useRef<{ lowHz: number; highHz: number } | null>(null);
  // Whether a finger is actually down, which `dragHz` alone can no longer say: it now
  // also holds a released-but-not-yet-confirmed width.
  const holdingRef = useRef(false);
  // The width the SESSION last reported, so the effect below fires on a change rather
  // than on every render that happens to pass the same number through.
  const confirmedRef = useRef(bandwidthHz);
  // The waterfall's own pixels, kept off-screen at one column per BIN and one row per
  // DEVICE pixel. Off-screen because the visible canvas is scrolled every frame and a
  // scrolled canvas no longer holds the numbers a resize or a colour-window change has
  // to redraw from — `historyRef` does, which is why both exist.
  const fallRef = useRef<HTMLCanvasElement | null>(null);
  const historyRef = useRef<SpectrumRow[]>([]);
  const paintedRef = useRef<{ scale: FallScale; bins: number; rows: number } | null>(null);
  const modeRef = useRef(mode);
  modeRef.current = mode;
  // The newest reading, for the repaint a MODE change triggers. A ref rather than the
  // state it mirrors: the trace wants the caret drawn on it, but listing `tuning` as a
  // dependency of that effect would re-run it ten times a second and fight the
  // row-driven repaint that already owns every other frame.
  const tuningRef = useRef<Tuning | null>(null);

  useEffect(() => {
    const draw = (next: SpectrumRow | null) => {
      const canvas = canvasRef.current;
      // Only a CHANNEL row. The same stream carries band rows from a spectrum session
      // on another radio, and one of those drawn here would be a picture of somewhere
      // else, centred on a frequency it does not contain.
      if (!canvas || !next || next.passbandHz <= 0) return;
      const finite = next.db.filter((v) => Number.isFinite(v));
      if (finite.length === 0) return;
      const sorted = [...finite].sort((a, b) => a - b);
      const low = sorted[Math.floor(sorted.length * 0.1)] as number;
      const high = (sorted[sorted.length - 1] as number) + HEADROOM_DB;
      const want = { lowDb: low, highDb: Math.max(high, low + MIN_SPAN_DB) };
      const held = scaleRef.current;
      const eased = {
        lowDb: held.lowDb + (want.lowDb - held.lowDb) * SCALE_EASE,
        highDb: held.highDb + (want.highDb - held.highDb) * SCALE_EASE,
      };
      scaleRef.current = eased;
      // Device pixels 1:1 with the box, so a row is never resampled across the
      // picture — the twinkle fix the waterfall needed, applied before it can happen.
      // The HEIGHT is device pixels too, which it was not: the backing store was 78
      // against a CSS box three times that on a phone, so the trace was stretched and a
      // waterfall row would have been smeared over three pixel rows with the smear
      // rotating as it scrolled — precisely the twinkle.
      const ratio = window.devicePixelRatio || 1;
      const wanted = Math.round(canvas.clientWidth * ratio);
      const tall = Math.round(CHART_H * ratio);
      if (wanted > 0 && canvas.width !== wanted) canvas.width = wanted;
      if (canvas.height !== tall) canvas.height = tall;
      // Averaged for the READING only — see READING_ROWS. Rows from a different band
      // are dropped rather than averaged with these, so a retune re-reads from the new
      // band's own frames instead of blending the two into a frequency neither is on.
      const recent = recentRef.current;
      if (recent.length > 0) {
        const first = recent[0] as SpectrumRow;
        if (first.startHz !== next.startHz || first.db.length !== next.db.length) {
          recent.length = 0;
        }
      }
      recent.push(next);
      if (recent.length > READING_ROWS) recent.shift();
      const mean = { ...next, db: next.db.map((_, i) => average(recent, i)) };
      const read = tuningOf(mean, frequencyHz);

      // The history is kept in BOTH modes so switching to the waterfall shows what the
      // channel has been doing, rather than starting a fresh picture from the moment
      // the owner happened to tap. One row per device pixel row is exactly what the
      // waterfall can show and no more; a retune drops it, since those rows belong to a
      // channel this strip no longer covers.
      const history = historyRef.current;
      const oldest = history[0];
      if (oldest && (oldest.startHz !== next.startHz || oldest.db.length !== next.db.length)) {
        history.length = 0;
        paintedRef.current = null;
      }
      history.unshift(next);
      if (history.length > canvas.height) history.length = canvas.height;

      if (modeRef.current === "fall") {
        let off = fallRef.current;
        if (!off) {
          off = document.createElement("canvas");
          fallRef.current = off;
        }
        paintedRef.current = fall(canvas, off, history, eased, paintedRef.current);
      } else {
        paintedRef.current = null;
        paint(canvas, next, read, eased);
      }
      guides(canvas, next, ratio, pendingRef.current);
      tuningRef.current = read;
      setRow(next);
      setTuning(read);
    };
    draw(sdrSpectrum().channel);
    // CHANNEL rows only: the session this strip watches now also publishes the whole
    // 2.4 MHz band it is sitting in, and this canvas is 32 kHz wide.
    return subscribeSdrSpectrum((_state, next) => {
      if (next && next.view !== "channel") return;
      draw(next);
    });
    // `mode` is read through `modeRef` rather than listed here: re-subscribing on a tap
    // would drop the stream and the history with it, and the whole point of keeping
    // the history in both modes is that the waterfall has something to show the moment
    // it is switched to.
  }, [frequencyHz]);

  // Repaint immediately on a tap rather than waiting for the next row: at 10 fps that
  // is a tenth of a second of the old picture, which reads as the tap not working.
  useEffect(() => {
    const canvas = canvasRef.current;
    const history = historyRef.current;
    const newest = history[0];
    if (!canvas || !newest) return;
    const ratio = window.devicePixelRatio || 1;
    if (mode === "fall") {
      let off = fallRef.current;
      if (!off) {
        off = document.createElement("canvas");
        fallRef.current = off;
      }
      paintedRef.current = fall(canvas, off, history, scaleRef.current, null);
    } else {
      paintedRef.current = null;
      paint(canvas, newest, tuningRef.current, scaleRef.current);
    }
    guides(canvas, newest, ratio, pendingRef.current);
  }, [mode]);

  const span = row && row.passbandHz > 0 ? row.stopHz - row.startHz : 0;
  const edge = span / 2;
  // Where the two draggable edges sit, as a percentage across the picture. Computed
  // from the mode and the width rather than read off the row so they track the finger
  // during a drag — the row only catches up when the box answers, ~100 ms later, and
  // handles that waited for it would feel broken rather than merely late.
  // The session has spoken: stop showing the dragged width and go back to describing
  // what the box actually did. This is also what stops a REFUSED width being drawn
  // forever — the session comes back carrying the old one, and the picture follows it.
  // Skipped while a finger is down, or a change landing mid-drag would yank the edge
  // out from under it.
  useEffect(() => {
    if (confirmedRef.current === bandwidthHz) return;
    confirmedRef.current = bandwidthHz;
    if (!holdingRef.current) setDragHz(null);
  }, [bandwidthHz]);

  // The zoom, which is a ladder rather than a range: a magnification has no equivalent
  // of "narrower than that station" to place by eye.
  const zoomable = onViewSpan !== undefined && viewSpans.length > 1;
  const stepZoom = () => {
    const at = viewSpans.indexOf(viewSpanHz);
    // Wraps rather than stopping, because a tap-to-cycle control that dead-ends looks
    // broken — there is no second button here to go back with.
    onViewSpan?.(viewSpans[(at + 1) % viewSpans.length] as number);
  };

  const dragging = onBandwidth !== undefined && span > 0;
  const shownHz = dragHz ?? bandwidthHz;
  const edges = dragging && demodMode ? bandwidthEdges(demodMode, shownHz) : null;
  // Held only while the row DISAGREES — see `pendingPassband` for why, and for why it
  // must stop the moment the row catches up.
  pendingRef.current =
    dragging && demodMode && row ? pendingPassband(demodMode, shownHz, row.passbandHz) : null;
  const pctOf = (hz: number) => 50 + (hz / span) * 100;
  return (
    <>
      <p className="sdr-label tv-label">
        Tuning
        <span className="tv-span">
          {span > 0 ? (
            <>
              {/* The label IS the zoom. A tap steps to the next width and wraps, which
                  is affordable here and nowhere else on this sheet: the box answers by
                  cropping the next frame, so there is no rebuild and no click — a
                  bandwidth button could not behave this way. */}
              {zoomable ? (
                <button type="button" className="tv-zoom" onClick={stepZoom}>
                  {kHz(span)} kHz view
                </button>
              ) : (
                `${kHz(span)} kHz view`
              )}
              {` · ${kHz(edges ? edges.highHz - edges.lowHz : (row?.passbandHz ?? 0))} kHz passband`}
            </>
          ) : (
            "waiting for the radio"
          )}
        </span>
      </p>
      <div className={`tv-chart${dragging ? " sdr-tuneview" : ""}`} ref={chartRef}>
        {tuning && <span className="tv-lvl">{tuning.peakDb.toFixed(1)} dBFS</span>}
        {/* The picture IS the control, which is what the owner asked for — "a waterfall
            version if I click it". Two readings of one channel: where the signal is
            now, and what it has been doing. Same footprint and same span either way, so
            nothing below moves when it changes. */}
        <button
          type="button"
          className="tv-swap"
          aria-pressed={mode === "fall"}
          aria-label={mode === "fall" ? "Show the live trace" : "Show the waterfall"}
          onClick={() => setMode((now) => (now === "fall" ? "trace" : "fall"))}
        >
          <span aria-hidden="true">{mode === "fall" ? "Trace" : "Waterfall"}</span>
        </button>
        {/* Named rather than hidden, the way the waterfall's is: the picture carries a
            reading, so a screen reader that skipped it would skip the answer. The
            sentence below is the same fact in words, which is what makes the label a
            summary and not the only copy of it. */}
        <canvas
          ref={canvasRef}
          height={CHART_H}
          role="img"
          aria-label={
            tuning
              ? `${offsetLabel(tuning)}, ${tuning.peakDb.toFixed(1)} dBFS`
              : "Nothing in this channel"
          }
        />
        {edges && demodMode && (
          <>
            {(["low", "high"] as const).map((side) => {
              // SSB's inner edge is the suppressed carrier's, not the width's: it does
              // not move as the filter narrows, so dragging it would ask a question the
              // value cannot answer.
              const fixed =
                (demodMode === "usb" && side === "low") || (demodMode === "lsb" && side === "high");
              const hz = side === "low" ? edges.lowHz : edges.highHz;
              return (
                <button
                  key={side}
                  type="button"
                  className={`sdr-edge${dragHz !== null ? " sdr-edge-on" : ""}`}
                  style={{ left: `${pctOf(hz)}%` }}
                  disabled={fixed}
                  aria-label={`${side === "low" ? "Lower" : "Upper"} filter edge, ${bandwidthSpoken(dragHz ?? bandwidthHz)}`}
                  onPointerDown={(e) => {
                    if (fixed) return;
                    e.currentTarget.setPointerCapture(e.pointerId);
                    holdingRef.current = true;
                    setDragHz(bandwidthHz);
                  }}
                  onPointerMove={(e) => {
                    if (dragHz === null || !chartRef.current) return;
                    const box = chartRef.current.getBoundingClientRect();
                    const offset = ((e.clientX - box.left) / box.width - 0.5) * span;
                    setDragHz(snapBandwidth(widthFromOffset(demodMode, offset), minHz, maxHz));
                  }}
                  onPointerUp={() => {
                    holdingRef.current = false;
                    // Committed on RELEASE, not at every kilohertz crossed: a drag across
                    // the range would otherwise rebuild the demodulator a dozen times,
                    // and each rebuild is an audible click.
                    if (dragHz !== null && dragHz !== bandwidthHz) onBandwidth?.(dragHz);
                    // `dragHz` deliberately SURVIVES the release. Clearing it here fell
                    // back to the session's width, which is still the old one until the
                    // retune lands ~100 ms later — so the edges sprang back to where they
                    // started and then jumped forward again, which reads as the drag
                    // having been rejected. It clears when the session catches up.
                  }}
                  onPointerCancel={() => {
                    holdingRef.current = false;
                    setDragHz(null);
                  }}
                  onKeyDown={(e) => {
                    // The arrow that moves this handle INWARD narrows, whichever handle
                    // it is — the gesture is "pull the edge in", not "decrease a number".
                    const inward = side === "high" ? "ArrowLeft" : "ArrowRight";
                    const outward = side === "high" ? "ArrowRight" : "ArrowLeft";
                    if (e.key !== inward && e.key !== outward) return;
                    const next = stepBandwidth(
                      shownHz,
                      e.key === inward ? "narrower" : "wider",
                      minHz,
                      maxHz,
                    );
                    if (next !== null) onBandwidth?.(next);
                    e.preventDefault();
                  }}
                />
              );
            })}
            {dragHz !== null && <span className="sdr-edge-live">{bandwidthSpoken(dragHz)}</span>}
          </>
        )}
      </div>
      <div className="tv-axis">
        {span > 0 ? (
          [-edge, -edge / 2, 0, edge / 2, edge].map((hz, i) => (
            <span key={hz}>
              {hz > 0 ? "+" : hz < 0 ? "−" : ""}
              {kHz(Math.abs(hz))}
              {i === 4 ? " kHz" : ""}
            </span>
          ))
        ) : (
          <span />
        )}
      </div>
      <p className="tv-status">
        {tuning ? (
          <>
            <span className={`dot${tuning.centred ? " on" : " warn"}`} />
            {offsetLabel(tuning)}
            {spillLabel(tuning)}
            {onTune && !tuning.centred && (
              <button
                type="button"
                className="tv-nudge"
                onClick={() => onTune(Math.round(frequencyHz + tuning.errorHz))}
              >
                Centre it
              </button>
            )}
          </>
        ) : (
          <>
            <span className="dot" />
            {span > 0
              ? "Nothing in this channel — what you can hear is noise."
              : "The picture starts with the audio."}
          </>
        )}
      </p>
    </>
  );
}
