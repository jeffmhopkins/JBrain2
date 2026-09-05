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
import {
  type SpectrumRow,
  type SpectrumState,
  sameBand,
  sdrSpectrum,
  subscribeSdrSpectrum,
} from "../sdrSpectrum";
import { type Scale, calibrate, calibrated, frameRate, historyRows, paint } from "../sdrWaterfall";

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

export function SdrWaterfall({ height = 220 }: { height?: number }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // What the axis under the picture reads. React state, because it changes on a retune
  // rather than on every row, and re-rendering two labels a minute costs nothing.
  const [band, setBand] = useState<SpectrumRow | null>(() => sdrSpectrum().latest);
  const [status, setStatus] = useState<SpectrumState>(() => sdrSpectrum());
  // The measured rate, for the note only — and set only when the rounded figure moves,
  // so a stream ten rows a second does not re-render this tree ten times a second.
  const [fps, setFps] = useState<number | null>(null);

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

    const fit = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round((canvas.clientWidth || 1) * ratio);
      canvas.height = Math.round((canvas.clientHeight || 1) * ratio);
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
      // Horizontally this is 1:1 — `bins` IS the device width, because `paint` already
      // reduced the frame's bins to columns by max-hold rather than leaving the choice
      // to a filter. Only the vertical is scaled, and only by a constant.
      ctx.imageSmoothingEnabled = true;
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
      return true;
    };

    const repaint = () => {
      if (bins === 0 || rows === 0 || scale === null) return;
      offCtx.putImageData(
        new ImageData(paint(history, bins, rows, scale, extent), bins, rows),
        0,
        0,
      );
      // `paint` lays the newest row at the bottom and the blanks above it — which is the
      // ring read from slot 0, so a full repaint is also how the head gets re-zeroed.
      head = 0;
    };

    const append = (row: SpectrumRow) => {
      if (scale === null) return;
      offCtx.putImageData(new ImageData(paint([row], bins, 1, scale, extent), bins, 1), 0, head);
      head = (head + 1) % rows;
    };

    const onResize = () => {
      fit();
      // The offscreen is display-width, so a resize changes what a column means. The
      // ring cannot be rescaled without inventing measurements, and `history` is
      // exactly what it is kept for.
      if (reshape(Math.max(1, canvas.width), rows)) repaint();
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
        setBand(row);
      }
      history.unshift(row);

      const rate = frameRate(history);
      // The measured rate drifts by a percent or two between frames, and re-sizing the
      // ring for that would repaint the whole picture every row for a height nobody can
      // see change. Only a real move is worth it: a tier change is a factor of ten.
      const wanted = historyRows(rate, row.db.length);
      const keep = rows === 0 || Math.abs(wanted - rows) > rows / 8 ? wanted : rows;
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
      let full = reshape(Math.max(1, canvas.width), keep);
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
      if (full) repaint();
      else append(row);
      show();
    });

    return () => {
      window.removeEventListener("resize", onResize);
      if (frame) cancelAnimationFrame(frame);
      unsubscribe();
    };
  }, []);

  const bins = band?.db.length ?? 0;
  return (
    <div className="wf">
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
      {band ? (
        <div className="wf-axis">
          <span>{mhz(band.startHz)}</span>
          <span>{mhz((band.startHz + band.stopHz) / 2)}</span>
          <span>{mhz(band.stopHz)}</span>
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
