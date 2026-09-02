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
// It draws only while mounted, i.e. only while the sheet is open, so a closed tuner
// costs nothing. That means the tape starts empty and fills from the right; there is
// no attempt to keep history while nobody is looking at it.

import { useEffect, useRef } from "react";
import { sdrAnalyser } from "../sdrAudio";

// Seconds of history at roughly one column per animation frame. Long enough that a
// transmission you glanced away from is still on screen, short enough that the
// columns stay wide enough to read on a phone.
const WINDOW_S = 12;
const COLUMNS = WINDOW_S * 60;
// Peak-normalised RMS is very quiet for speech; this lifts a normal signal to most of
// the height without clipping a loud one, matching what the mock was tuned against.
const GAIN = 2.6;

interface Props {
  /** Paused radio freezes the tape rather than scrolling silence past. */
  playing: boolean;
}

export function SdrTape({ playing }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const historyRef = useRef<Float32Array>(new Float32Array(COLUMNS));
  const atRef = useRef(0);
  // Read inside the animation frame instead of closing over it, so toggling play does
  // not restart the loop and lose the history already on screen.
  const playingRef = useRef(playing);
  playingRef.current = playing;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx2d = canvas.getContext("2d");
    if (!ctx2d) return;

    const history = historyRef.current;
    let samples: Uint8Array<ArrayBuffer> | null = null;
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
      const analyser = sdrAnalyser();

      if (analyser && playingRef.current) {
        if (!samples || samples.length !== analyser.fftSize) {
          samples = new Uint8Array(new ArrayBuffer(analyser.fftSize));
        }
        analyser.getByteTimeDomainData(samples);
        let sum = 0;
        for (let i = 0; i < samples.length; i += 1) {
          const centred = ((samples[i] ?? 128) - 128) / 128;
          sum += centred * centred;
        }
        history[atRef.current] = Math.min(1, Math.sqrt(sum / samples.length) * GAIN);
        atRef.current = (atRef.current + 1) % COLUMNS;
      }

      ctx2d.clearRect(0, 0, width, height);
      const mid = height / 2;
      ctx2d.strokeStyle = rule;
      ctx2d.lineWidth = 1;
      ctx2d.beginPath();
      ctx2d.moveTo(0, mid + 0.5);
      ctx2d.lineTo(width, mid + 0.5);
      ctx2d.stroke();

      const shown = Math.min(COLUMNS, Math.max(1, Math.floor(width)));
      const step = width / shown;
      ctx2d.fillStyle = ink;
      for (let i = 0; i < shown; i += 1) {
        // Newest at the right edge, so the tape reads the way time does.
        const index = (atRef.current - 1 - (shown - 1 - i) + COLUMNS * 2) % COLUMNS;
        const level = history[index] || 0;
        const bar = Math.max(1, level * (height - 4));
        ctx2d.globalAlpha = 0.35 + Math.min(0.65, level * 2.2);
        ctx2d.fillRect(i * step, mid - bar / 2, Math.max(1, step - 0.4), bar);
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
      aria-label={`Audio level over the last ${WINDOW_S} seconds`}
    />
  );
}
