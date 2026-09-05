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
import { type SpectrumRow, sdrSpectrum, subscribeSdrSpectrum } from "../sdrSpectrum";
import { type Tuning, offsetLabel, spillLabel, tuningOf } from "../sdrTuning";

/** Device pixels of chart height. Tall enough to read a shoulder, short enough to
 *  leave the transport on screen without scrolling. */
const CHART_H = 78;

/** How much of the row's own dynamic range the picture spans, and the floor it never
 *  collapses below. A channel with nothing in it has almost no spread, and a scale
 *  stretched across that turns rounding into a light show — the same reason
 *  `sdrWaterfall.calibrate` has a `MIN_SPAN_DB`. */
const MIN_SPAN_DB = 24;
const HEADROOM_DB = 4;

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

  // The passband, centred on the tuned frequency — which is the row's own middle.
  const centre = row.startHz + span / 2;
  const left = xOf(centre - row.passbandHz / 2);
  const right = xOf(centre + row.passbandHz / 2);
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

function kHz(hz: number): string {
  const value = hz / 1000;
  return Number.isInteger(value) ? `${value}` : value.toFixed(1);
}

export function SdrTuningView({
  frequencyHz,
  onTune,
}: {
  /** What the radio is tuned to. Passed rather than read off the row: the row is
   *  cropped to whole bins, so its midpoint can sit half a bin from the frequency. */
  frequencyHz: number;
  /** Retune to where the signal actually is. Absent when the surface cannot retune. */
  onTune?: (hz: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const scaleRef = useRef<Scale>({ lowDb: -90, highDb: -20 });
  // The row is held in a ref and painted imperatively — at 10 fps, putting a few
  // hundred numbers through React state would re-render the whole surface for a
  // picture that is drawn on a canvas anyway. Only the SENTENCE is state, and it
  // changes when the reading does rather than when the row does.
  const [tuning, setTuning] = useState<Tuning | null>(null);
  const [row, setRow] = useState<SpectrumRow | null>(null);

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
      const wanted = Math.round(canvas.clientWidth * (window.devicePixelRatio || 1));
      if (wanted > 0 && canvas.width !== wanted) canvas.width = wanted;
      if (canvas.height !== CHART_H) canvas.height = CHART_H;
      const read = tuningOf(next, frequencyHz);
      paint(canvas, next, read, eased);
      setRow(next);
      setTuning(read);
    };
    draw(sdrSpectrum().latest);
    return subscribeSdrSpectrum((_state, next) => draw(next));
  }, [frequencyHz]);

  const span = row && row.passbandHz > 0 ? row.stopHz - row.startHz : 0;
  const edge = span / 2;
  return (
    <>
      <p className="sdr-label tv-label">
        Tuning
        <span className="tv-span">
          {span > 0
            ? `${kHz(span)} kHz view · ${kHz(row?.passbandHz ?? 0)} kHz passband`
            : "waiting for the radio"}
        </span>
      </p>
      <div className="tv-chart">
        {tuning && <span className="tv-lvl">{tuning.peakDb.toFixed(1)} dBFS</span>}
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
                onClick={() => onTune(Math.round(frequencyHz + tuning.offsetHz))}
              >
                Centre it
              </button>
            )}
          </>
        ) : (
          <>
            <span className="dot" />
            {span > 0 ? "Nothing in this channel." : "The picture starts with the audio."}
          </>
        )}
      </p>
    </>
  );
}
