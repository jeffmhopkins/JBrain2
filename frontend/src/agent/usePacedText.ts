// Decouple the streamed answer's *reveal* cadence from its network arrival, so a
// fast model's big chunks read as steady typing rather than jumpy block-paste. The
// caller hands us `target` (everything received so far) and whether the turn is
// still streaming; we return the prefix that has been "typed" so far, catching up
// to `target` at the user's configured tokens/second (tokenRate.ts). Once the turn
// settles, motion is reduced, or pacing is off, the full text shows at once — so a
// replayed transcript and a short answer never lag behind themselves.

import { useEffect, useRef, useState } from "react";
import { getTokenRate } from "../tokenRate";

// Tokens are a feel knob, not a billing count — convert to characters at a rough
// English average so the rate maps to a visible reveal speed.
const CHARS_PER_TOKEN = 4;

function instant(): boolean {
  return (
    getTokenRate() === 0 ||
    // jsdom has no matchMedia — optional-chain so the hook is test-safe.
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true
  );
}

export function usePacedText(target: string, streaming: boolean): string {
  const [shownLen, setShownLen] = useState(0);
  // Fractional accumulator so a low rate still advances a fraction of a char per
  // frame; the rendered length is its floor.
  const shown = useRef(0);
  const targetRef = useRef(target);
  const raf = useRef<number | null>(null);
  const last = useRef<number | null>(null);
  targetRef.current = target;

  // Pace only an actively streaming turn (and only when pacing is on). A settled
  // turn — including a replayed transcript — and the instant/reduced-motion cases
  // resolve to the full text at render time, with no effect-driven lag.
  const pacing = streaming && !instant();

  // Drive the drip while pacing; the loop reads targetRef live, so growing text
  // doesn't tear down and restart the loop (which would stutter the cadence). `target`
  // is a dep on purpose — not read in the body, but each new delta must re-run this to
  // re-kick the loop after it idled at a caught-up length.
  // biome-ignore lint/correctness/useExhaustiveDependencies: target re-kicks an idled drip
  useEffect(() => {
    if (!pacing || raf.current !== null) return;
    const tick = (now: number) => {
      const len = targetRef.current.length;
      if (shown.current >= len) {
        // Caught up — idle until the next delta re-runs this effect and re-kicks us.
        raf.current = null;
        last.current = null;
        return;
      }
      if (last.current === null) last.current = now;
      const dt = (now - last.current) / 1000;
      last.current = now;
      shown.current = Math.min(len, shown.current + getTokenRate() * CHARS_PER_TOKEN * dt);
      setShownLen(Math.floor(shown.current));
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
  }, [target, pacing]);

  // Stop dripping the instant pacing ends (settle / instant / unmount).
  useEffect(() => {
    if (pacing) return;
    if (raf.current !== null) {
      cancelAnimationFrame(raf.current);
      raf.current = null;
    }
    last.current = null;
  }, [pacing]);

  useEffect(
    () => () => {
      if (raf.current !== null) cancelAnimationFrame(raf.current);
    },
    [],
  );

  return pacing ? target.slice(0, Math.min(shownLen, target.length)) : target;
}
