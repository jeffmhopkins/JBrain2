// The tuner's rolling tape — loudness over the last few seconds, newest at the right.
//
// Chosen from three mocks (docs/mocks/sdr-tuner/d-waveform-tape.html is the binding
// spec). The argument for history over an instantaneous trace is that scanner traffic
// is BURSTY: the question while listening is usually "did anything just come through",
// which a scope answers only if you happen to be looking at the exact moment. A tape
// still answers it four seconds later.
//
// It sits INSIDE the transport row rather than above it, so it costs no vertical space
// of its own on a phone — the row was already as tall as the play button.
//
// It DRAWS only while mounted, but it does not SAMPLE: the history lives in
// sdrAudio.ts and is recorded for as long as the radio plays, sheet open or not.
// Opening the tuner should show what already happened — the owner asked for exactly
// that, and a tape that starts recording when you look at it answers the wrong
// question on a channel whose traffic arrives in bursts.

import { useEffect, useRef } from "react";
import { TAPE_WINDOW_S, sdrLevels } from "../sdrAudio";

export function SdrTape() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return;

    let frame = 0;
    let width = 0;
    let height = 0;

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      width = canvas.clientWidth || 1;
      height = canvas.clientHeight || 1;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      ctx2d.setTransform(ratio, 0, 0, ratio, 0, 0);
    };
    resize();
    window.addEventListener("resize", resize);

    // Read the theme's own tokens rather than hardcoding: the sheet is themed, and a
    // literal here would be one more colour to forget when the palette moves.
    const styles = getComputedStyle(canvas);
    const ink = styles.getPropertyValue("--accent").trim() || "#7FA7C9";
    const rule = styles.getPropertyValue("--border").trim() || "#26282C";

    const draw = () => {
      frame = requestAnimationFrame(draw);
      const { levels, at, length } = sdrLevels();

      ctx2d.clearRect(0, 0, width, height);
      const mid = height / 2;
      ctx2d.strokeStyle = rule;
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.moveTo(0, mid + 0.5);
      ctx2d.lineTo(width, mid + 0.5);
      ctx2d.stroke();

      // One column per sample rather than per pixel: at 20 Hz the whole window is a
      // few hundred columns, so each gets real width instead of a hairline.
      const shown = Math.min(length, Math.max(1, Math.floor(width / 3)));
      const step = width / shown;
      ctx2d.fillStyle = ink;
      for (let i = 0; i < shown; i += 1) {
        // Newest at the right edge, so the tape reads the way time does.
        const index = (at - 1 - (shown - 1 - i) + length * 2) % length;
        const level = levels[index] || 0;
        const bar = Math.max(1, level * (height - 8));
        ctx2d.globalAlpha = 0.35 + Math.min(0.65, level * 2.2);
        ctx2d.fillRect(i * step, mid - bar / 2, Math.max(1, step - 0.8), bar);
      }
      ctx2d.globalAlpha = 1;
    };
    frame = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="sdr-tape"
      role="img"
      aria-label={`Audio level over the last ${TAPE_WINDOW_S} seconds`}
    />
  );
}
