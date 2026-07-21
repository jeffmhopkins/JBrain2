// Native back = climb one level, the same as the swipe-down every stacked layer
// already honors (docs/reference/DESIGN.md navigation tree). The app keeps no URL router,
// so the OS/browser back gesture would otherwise leave the PWA instead of popping a
// layer. We keep a single "trap" entry permanently at the top of the History API: every
// back consumes it, we immediately re-arm it, and we climb one on-screen layer. At the
// root there's nothing left to climb, so back simply stays put — the gesture never exits
// the app (exit is via the home button / app switcher).
//
// A permanent trap (rather than one mirrored on and off as layers open and close) makes
// this robust to any layer the depth count doesn't see, and to a close that swaps one
// layer for another rather than decrementing (Tasks' "return to the task card"): a
// miscount can now only ever leave a back gesture as a no-op, never as an accidental
// exit. It also drops the on-close history unwinding the mirrored trap needed, so the
// history stack and the on-screen stack can't drift.

import { useEffect, useRef } from "react";

/** Wire the platform back gesture to `onBack` while `depth` layers are open. `onBack`
 * must close exactly one layer (the topmost), like the swipe-down. Back never exits the
 * app: at depth 0 the gesture is consumed and does nothing. */
export function useBackGesture(depth: number, onBack: () => void): void {
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;
  const depthRef = useRef(depth);
  depthRef.current = depth;

  useEffect(() => {
    // Arm the permanent trap once so the very first back is already caught.
    window.history.pushState({ jbBack: true }, "");
    function onPop() {
      // The back consumed our trap — re-arm immediately so the next one is caught too,
      // then climb one layer. At the root (depth 0) there's nothing to climb, which is
      // what keeps the gesture inside the app.
      window.history.pushState({ jbBack: true }, "");
      if (depthRef.current > 0) onBackRef.current();
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
}
