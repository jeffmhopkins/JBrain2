// The live waterfall: newest row at the bottom, history rising.
//
// The rows come off `sdrSpectrum.ts` and the colours off `sdrWaterfall.ts`; this draws
// them and nothing else. Deliberately thin, because the two halves that can be silently
// wrong — the colour window and the bin-to-pixel mapping — are both testable arithmetic
// and neither of them needs a canvas to be checked.
//
// **The row rate is the stream's, not this file's**, and the picture says which it is.
// `rtl_power` measures a whole span by retuning within each one-second interval, so a
// span needing several hops watches any given frequency for a fraction of that second
// (bands.py, `duty`); the I/Q engine holds one hop at ~10 fps. Nothing here is written
// in rows — how much history is kept and how long the colour scale is taken over are
// both seconds, converted at the rate the rows themselves report.
//
// **A ring buffer, not a repaint.** This was drawn by re-painting the whole history
// into an ImageData every row, and the header used to defend that against self-blitting
// on the grounds that a scrolled canvas no longer holds the numbers a resize, a theme
// change or a retune has to redraw from. That reasoning was right and the conclusion
// does not follow, because the choice was never either/or: at 4096 bins and three
// minutes of a 10 fps stream a full repaint is 7.4 M `shade()` calls and 29 MB of
// garbage EVERY row, on a phone's main thread. So the picture is kept BOTH ways — the
// numbers in `history` for the redraws that need re-colouring, the pixels in an
// offscreen ring for the ones that do not:
//
//   a new row          one row of `shade()` into the ring slot the oldest row occupied
//   a resize           no repaint at all; the ring is blitted to the new canvas size
//   a theme change     no repaint at all; unpainted pixels are transparent and the CSS
//                      background shows through (`.wf-canvas`)
//   a new colour scale full repaint from `history`, which only happens while calibrating
//   a retune           the picture is blanked and starts again, as it always did
//
// And painting is decoupled from arrival: rows land in the ring as they come, but the
// visible canvas is redrawn at most once an animation frame. Dropping a paint is free —
// the ring already holds the row — while dropping a row would be a hole in the time base.

import { useEffect, useRef, useState } from "react";
import { khz, mhz } from "../mhz";
import { type HeldPeak, mergePeaks, positionOf, visiblePeaks } from "../sdrPeaks";
import {
  type SpectrumRow,
  type SpectrumState,
  sameBand,
  sdrSpectrum,
  subscribeSdrSpectrum,
} from "../sdrSpectrum";
import {
  type Scale,
  calibrate,
  calibrated,
  frameRate,
  holdInto,
  paint,
  shadeRow,
  stackFor,
} from "../sdrWaterfall";

/** Whether a provisional colour window is worth re-taking at this history length.
 *
 *  Powers of two, so the window is re-taken on a DOUBLING of the sample rather than on
 *  every row. It converges as the sample grows — the eightieth row of a 10 fps stream
 *  moves a percentile by a fraction of a dB — and every re-take costs a full repaint,
 *  so re-taking per row would spend the whole first eight seconds doing the work this
 *  file exists to stop doing. Eight doublings covers any rate the box can produce. */
function worthRecalibrating(rows: number): boolean {
  return (rows & (rows - 1)) === 0;
}

/** How the note under the picture says the rate, once enough rows have shown one. */
function rateNote(fps: number): string {
  return fps >= 1.5 ? `${Math.round(fps)} rows a second` : "one row a second";
}

/** How often the listed levels refresh when nothing has appeared or stopped. The set
 *  changing is what moves a marker; this is only so a dB reading is not a minute old. */
const PEAKS_HEARTBEAT_S = 1;

export function SdrWaterfall({ height = 220 }: { height?: number }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // What the axis under the picture reads. React state, because it changes on a retune
  // rather than on every row, and re-rendering two labels a minute costs nothing.
  const [band, setBand] = useState<SpectrumRow | null>(() => sdrSpectrum().latest);
  const [status, setStatus] = useState<SpectrumState>(() => sdrSpectrum());
  // The measured rate, for the note only — and set only when the rounded figure moves,
  // so a stream ten rows a second does not re-render this tree ten times a second.
  const [fps, setFps] = useState<number | null>(null);
  // What is on the air, and which reading of it is on screen. `held` is the whole set
  // including what has stopped; the toggle chooses what is drawn from it.
  const [signals, setSignals] = useState<HeldPeak[]>([]);
  const [marks, setMarks] = useState<"off" | "live" | "held">("held");

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    // jsdom has no 2d context, and a box with a very old browser has none either. The
    // axis and the status line below still render, so the surface degrades to text.
    if (!ctx) return;

    // The picture at its NATURAL size — one pixel per bin, one per row — then scaled up
    // by `drawImage`. Building the ImageData at display size instead would mean
    // recomputing every colour on a resize, for a picture whose numbers did not change.
    const off = document.createElement("canvas");
    const offCtx = off.getContext("2d");
    if (!offCtx) return;
    // The ring unwrapped into reading order, so the display draw is a single scale
    // rather than two that move against each other. Sized in `blit`.
    const flat = document.createElement("canvas");
    const flatCtx = flat.getContext("2d");
    if (!flatCtx) return;

    let history: SpectrumRow[] = [];
    let scale: Scale | null = null;
    let frozen = false;
    let bins = 0;
    let rows = 0;
    // The band's full width in BINS, which `bins` no longer is — that is the
    // picture's width in device pixels now. Kept apart because a frame that lost a
    // block arrives short, and its tail has to stay transparent rather than be
    // stretched over the picture (`reduce`'s `extent`).
    let extent = 0;
    // The ring's write head: the slot the next row goes in, which is also the slot the
    // OLDEST row is in, which is therefore the top of the picture. One index carries all
    // three because a full ring is exactly that identity.
    let head = 0;
    let frame = 0;
    let saidFps: number | null = null;
    // The signals, held across rows here rather than in React: merging is per row and
    // cheap, publishing is a re-render and is not. Kept in the effect for the same
    // reason the ring is — the picture is on a canvas precisely so that ten rows a
    // second cost ten paints and no reconciliation.
    let held: HeldPeak[] = [];
    let saidKey = "";
    let saidAt = 0;
    // How many arriving rows share one row of pixels, and the max-hold they are
    // collected into. A row is never dropped: it lands in `pending` and reaches the
    // picture when its group completes, which is what keeps 180 s of history on a
    // display that has no 1800 pixel rows to put it in.
    let stack = 1;
    let pending: Float64Array | null = null;
    let pendingCount = 0;

    const fit = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      // `clientHeight` is 0 until the box has been laid out, and the ring is sized from
      // it now — so falling back to 1 would build a one-pixel-tall picture and group the
      // whole history into it, then regroup once layout arrived. The prop IS the CSS
      // height, so it is the right answer before layout can give one.
      canvas.width = Math.round((canvas.clientWidth || 1) * ratio);
      canvas.height = Math.round((canvas.clientHeight || height) * ratio);
    };

    const blit = () => {
      if (bins === 0 || rows === 0) return;
      // **Unwrap the ring 1:1 first, then scale ONCE.** The obvious version draws the
      // ring's two arcs straight to the display, and that is what made a still picture
      // twinkle: the split moves down one row every frame, so each half's vertical
      // scale factor — and with it the resampler's phase — changed on every paint. A
      // row whose numbers had not moved was resampled differently each time.
      //
      // Here both arc copies are exact (same width, same height, no filter can run),
      // and the single draw that follows has a scale factor of `rows / height` that
      // does not depend on `head` at all. What is on screen can then only change when
      // the measurements do.
      flat.width = bins;
      flat.height = rows;
      const above = rows - head;
      if (above > 0) flatCtx.drawImage(off, 0, head, bins, above, 0, 0, bins, above);
      if (head > 0) flatCtx.drawImage(off, 0, 0, bins, head, 0, above, bins, head);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      // 1:1 on BOTH axes now, which is the only thing that makes a scrolling picture
      // stable. `bins` is the device width because `paint` reduces bins to columns
      // itself, and `rows` is the device height because it reduces arriving rows to
      // pixel rows itself (`stackFor`). A constant scale factor was not enough and the
      // owner saw why: it fixes the MAPPING, but the data moves through it — every row
      // shifts down one source row each frame, so a filter's blend membership rotates
      // and a row whose numbers never changed is drawn differently each time.
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(flat, 0, 0, bins, rows, 0, 0, canvas.width, canvas.height);
    };

    // One paint a frame at most. `frame` doubles as the "already asked" flag, so rows
    // arriving faster than the display coalesce into the single paint that shows them.
    const show = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        blit();
      });
    };

    /** Resize the ring, which clears it. True when the caller must repaint. */
    const reshape = (nextBins: number, nextRows: number): boolean => {
      if (off.width === nextBins && off.height === nextRows) return false;
      off.width = nextBins;
      off.height = nextRows;
      bins = nextBins;
      rows = nextRows;
      // A half-collected group belongs to the old geometry: its columns were reduced to
      // a width that no longer exists.
      pending = null;
      pendingCount = 0;
      return true;
    };

    const repaint = () => {
      if (bins === 0 || rows === 0 || scale === null) return;
      offCtx.putImageData(
        new ImageData(paint(history, bins, rows, scale, extent, stack), bins, rows),
        0,
        0,
      );
      // `paint` lays the newest row at the bottom and the blanks above it — which is the
      // ring read from slot 0, so a full repaint is also how the head gets re-zeroed.
      // The grouping restarts with it, which is invisible and only happens on a resize
      // or a recalibration — both of which rebuild the whole picture anyway.
      head = 0;
      pending = null;
      pendingCount = 0;
    };

    const append = (row: SpectrumRow) => {
      if (scale === null) return;
      if (pending === null) {
        pending = new Float64Array(bins).fill(Number.NEGATIVE_INFINITY);
        pendingCount = 0;
      }
      holdInto(pending, row.db, bins, extent);
      pendingCount += 1;
      // Held back until the group is full, so a pixel row is always the same number of
      // measurements. Writing early and overwriting would make the newest row's colour
      // change under the viewer as its group filled — a twinkle of its own.
      if (pendingCount < stack) return;
      offCtx.putImageData(new ImageData(shadeRow(pending, bins, scale), bins, 1), 0, head);
      head = (head + 1) % rows;
      pending = null;
      pendingCount = 0;
    };

    const onResize = () => {
      fit();
      // The offscreen is display-width, so a resize changes what a column means. The
      // ring cannot be rescaled without inventing measurements, and `history` is
      // exactly what it is kept for.
      if (reshape(Math.max(1, canvas.width), Math.max(1, canvas.height))) repaint();
      show();
    };
    fit();
    window.addEventListener("resize", onResize);

    const unsubscribe = subscribeSdrSpectrum((next, row) => {
      setStatus(next);
      if (!row) return;
      // A retune arrives with no message of its own — the row simply describes another
      // band. Everything about the old picture is then wrong: its history is a
      // different frequency and its colour window is a different noise floor.
      if (!sameBand(history[0] ?? null, row)) {
        history = [];
        extent = 0;
        scale = null;
        frozen = false;
        // The signals were the OLD band's. Holding them across a retune would draw
        // markers at frequencies this picture does not cover.
        held = [];
        saidKey = "";
        setSignals([]);
        setBand(row);
      }
      history.unshift(row);

      const rate = frameRate(history);
      // The ring is the DISPLAY's height now, one pixel row per slot, because a
      // scrolling picture is only stable when the draw is 1:1 — see `blit`. The history
      // depth rides in `stack` instead: how many arriving rows share a pixel row.
      //
      // The measured rate drifts by a percent or two between frames, and re-grouping for
      // that would repaint the whole picture every row for a change nobody can see. Only
      // a real move is worth it: a tier change is a factor of ten.
      const deep = stackFor(rate, Math.max(1, canvas.height));
      const regroup = stack === 0 || Math.abs(deep - stack) > Math.max(1, stack / 4);
      const nextStack = regroup ? deep : stack;
      const keep = nextStack * Math.max(1, canvas.height);
      if (history.length > keep) history.length = keep;
      const said = rate === null ? null : Math.round(rate);
      if (said !== saidFps) {
        saidFps = said;
        setFps(rate);
      }

      // A rate learned mid-stream changes how tall the picture is, and the ring cannot
      // be resized without losing it — which is what `history` is kept for.
      // The picture is built at DISPLAY width now, not at bin width: `paint` reduces
      // bins to columns itself (max-hold), so nothing downstream has to guess which of
      // five bins a pixel means. A resize therefore reshapes and repaints, which is the
      // cost of that — paid on a resize rather than on every frame.
      let full = reshape(Math.max(1, canvas.width), Math.max(1, canvas.height));
      if (nextStack !== stack) {
        stack = nextStack;
        full = true;
      }
      // A band's widest row is its extent: a frame that lost a block is short, and the
      // one after it usually is not. Taking the max means a dropped block leaves a gap
      // rather than resizing the whole picture around its absence.
      if (row.db.length > extent) {
        extent = row.db.length;
        full = true;
      }
      if (!frozen) {
        if (calibrated(history)) {
          frozen = true;
          scale = calibrate(history);
          full = true;
        } else if (scale === null || worthRecalibrating(history.length)) {
          scale = calibrate(history);
          full = true;
        }
      }
      held = mergePeaks(held, row);
      // Published when the SET changes — a signal appearing or stopping is when a marker
      // has to move — plus a slow heartbeat so the listed levels do not go stale. A row
      // that only changed a decibel redraws nothing, which is the point.
      const key = held.map((peak) => `${peak.hz}:${peak.live ? 1 : 0}`).join(",");
      if (key !== saidKey || row.at - saidAt >= PEAKS_HEARTBEAT_S) {
        saidKey = key;
        saidAt = row.at;
        setSignals(held);
      }

      if (full) repaint();
      else append(row);
      show();
    });

    return () => {
      window.removeEventListener("resize", onResize);
      if (frame) cancelAnimationFrame(frame);
      unsubscribe();
    };
    // `height` is read by `fit` before layout can answer, so the effect depends on it.
    // It is a constant in practice — the prop has a default and no caller varies it —
    // so this re-runs never rather than on every render.
  }, [height]);

  const bins = band?.db.length ?? 0;
  // Markers are DOM over the picture, not pixels in it. The ring is drawn 1:1 on both
  // axes so that a scroll cannot resample it, and painting labels into that would put
  // text through the same transform — while a positioned overlay gets real type, moves
  // only when a signal does, and cannot disturb the picture it sits on.
  const shown = band && marks !== "off" ? visiblePeaks(signals, marks) : [];
  const placed = band
    ? shown
        .map((peak) => ({ peak, at: positionOf(peak.hz, band) }))
        .filter((m): m is { peak: HeldPeak; at: number } => m.at !== null)
    : [];
  return (
    <div className="wf">
      <div className="wf-stack">
        <canvas
          ref={canvasRef}
          className="wf-canvas"
          style={{ height }}
          role="img"
          aria-label={
            band
              ? `Live spectrum, ${mhz(band.startHz)} to ${mhz(band.stopHz)} megahertz`
              : "Live spectrum, waiting for the first row"
          }
        />
        {placed.length ? (
          <div className="wf-marks" aria-hidden="true">
            {placed.map(({ peak, at }) => (
              <span
                key={peak.hz}
                className={peak.live ? "wf-mark" : "wf-mark held"}
                style={{ left: `${at * 100}%` }}
              >
                <b>{mhz(peak.hz)}</b>
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {band ? (
        <div className="wf-axis">
          <span>{mhz(band.startHz)}</span>
          <span>{mhz((band.startHz + band.stopHz) / 2)}</span>
          <span>{mhz(band.stopHz)}</span>
        </div>
      ) : null}
      {band ? (
        <div className="wf-sig">
          <div className="wf-sigbar">
            <span className="wf-sightitle">
              Signals
              {marks === "off" ? null : <span className="wf-sigcount">{placed.length}</span>}
            </span>
            <fieldset className="wf-seg">
              <legend>Which signals to mark</legend>
              {(["off", "live", "held"] as const).map((option) => (
                <button
                  key={option}
                  type="button"
                  className={marks === option ? "on" : undefined}
                  aria-pressed={marks === option}
                  onClick={() => setMarks(option)}
                >
                  {option === "off" ? "Off" : option === "live" ? "Live" : "Held"}
                </button>
              ))}
            </fieldset>
          </div>
          {marks === "off" ? null : placed.length ? (
            <ul className="wf-siglist">
              {placed.map(({ peak }) => (
                <li key={peak.hz} className={peak.live ? undefined : "held"}>
                  <span className="wf-sighz">{mhz(peak.hz)}</span>
                  {/* Over the noise AROUND it, which is what made it a signal — an
                      absolute dBFS says nothing without the floor beside it. */}
                  <span className="wf-sigover">+{peak.overDb.toFixed(1)} dB</span>
                  {peak.live ? null : <span className="wf-sigheld">gone</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="wf-signone">
              {marks === "live"
                ? "Nothing above the noise right now."
                : "Nothing above the noise recently."}
            </p>
          )}
        </div>
      ) : null}
      <p className="wf-note">
        {status.error ? (
          <span className="wf-bad">{status.error}</span>
        ) : band ? (
          // The measurement's own terms, because they are what makes the picture
          // readable: how wide a column is, and how often a row lands. The rate is left
          // off until the rows have shown one rather than guessed at, because which
          // engine is behind the stream is exactly what it would be guessing.
          <>
            {bins} bins of {khz(band.binHz)} kHz
            {fps === null ? null : ` · ${rateNote(fps)}`}
          </>
        ) : (
          "Waiting for the first row…"
        )}
      </p>
    </div>
  );
}
