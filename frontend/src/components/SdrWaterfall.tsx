// The live waterfall: one row a second, newest at the bottom, history rising.
//
// The rows come off `sdrSpectrum.ts` and the colours off `sdrWaterfall.ts`; this draws
// them and nothing else. Deliberately thin, because the two halves that can be silently
// wrong — the colour window and the bin-to-pixel mapping — are both testable arithmetic
// and neither of them needs a canvas to be checked.
//
// **It is 1 fps, and it says so.** `rtl_power` measures a whole span by retuning within
// each one-second interval, so a span needing several hops watches any given frequency
// for a fraction of that second (bands.py, `duty`). A picture that hid that would look
// identical to one that could not miss a burst.
//
// Drawn from a HISTORY rather than by scrolling the canvas into itself. Self-blitting
// is the classic trick and it is a trap here: the picture has to survive a resize, a
// theme change and a retune, and each of those is a redraw the scrolled canvas cannot
// produce because it no longer holds the numbers.

import { useEffect, useRef, useState } from "react";
import { khz, mhz } from "../mhz";
import {
  type SpectrumRow,
  type SpectrumState,
  sameBand,
  sdrSpectrum,
  subscribeSdrSpectrum,
} from "../sdrSpectrum";
import { CALIBRATION_ROWS, type Scale, calibrate, paint } from "../sdrWaterfall";

/** How much history the picture holds. Three minutes at one row a second: long enough
 *  that a net or a repeater conversation is visible as a shape, short enough that a
 *  phone's canvas stays small. */
const ROWS = 180;

export function SdrWaterfall({ height = 220 }: { height?: number }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // What the axis under the picture reads. React state, because it changes on a retune
  // rather than on every row, and re-rendering two labels a minute costs nothing.
  const [band, setBand] = useState<SpectrumRow | null>(() => sdrSpectrum().latest);
  const [status, setStatus] = useState<SpectrumState>(() => sdrSpectrum());

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

    let history: SpectrumRow[] = [];
    let scale: Scale | null = null;
    let width = 0;
    let shown = 0;

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      width = canvas.clientWidth || 1;
      shown = canvas.clientHeight || 1;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(shown * ratio);
    };

    const blit = () => {
      const bins = history[0]?.db.length ?? 0;
      if (bins === 0 || scale === null) return;
      if (off.width !== bins || off.height !== ROWS) {
        off.width = bins;
        off.height = ROWS;
      }
      offCtx.putImageData(new ImageData(paint(history, bins, ROWS, scale), bins, ROWS), 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      // Smoothed, not nearest: a bin is rarely a whole number of pixels wide, and hard
      // edges on a resampled column read as structure the radio did not measure.
      ctx.imageSmoothingEnabled = true;
      ctx.drawImage(off, 0, 0, bins, ROWS, 0, 0, canvas.width, canvas.height);
    };

    const onResize = () => {
      resize();
      blit();
    };
    resize();
    window.addEventListener("resize", onResize);

    const unsubscribe = subscribeSdrSpectrum((next, row) => {
      setStatus(next);
      if (!row) return;
      // A retune arrives with no message of its own — the row simply describes another
      // band. Everything about the old picture is then wrong: its history is a
      // different frequency and its colour window is a different noise floor.
      if (!sameBand(history[0] ?? null, row)) {
        history = [];
        scale = null;
        setBand(row);
      }
      history.unshift(row);
      if (history.length > ROWS) history.length = ROWS;
      // Calibrated once and then held, so the picture does not renormalise around
      // whatever is on the air (see `sdrWaterfall.CALIBRATION_ROWS`). Until it is taken,
      // a provisional window off what has arrived keeps the first seconds visible.
      if (scale === null || history.length <= CALIBRATION_ROWS) scale = calibrate(history);
      blit();
    });

    return () => {
      window.removeEventListener("resize", onResize);
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
          // readable: how wide a column is, and that a row is a second.
          <>
            {bins} bins of {khz(band.binHz)} kHz · one row a second
          </>
        ) : (
          "Waiting for the first row…"
        )}
      </p>
    </div>
  );
}
