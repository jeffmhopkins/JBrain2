// Native back = climb one level, the same as the swipe-down every stacked layer
// already honors (docs/reference/DESIGN.md navigation tree). The app keeps no URL router,
// so the OS/browser back gesture would otherwise leave the PWA instead of
// popping a layer. We mirror the layer stack into the History API: a single
// "trap" entry sits in history while anything is open, the OS back consumes it
// and we close the topmost layer, re-arming the trap while deeper layers remain.
// A layer closed by the UI (swipe/chevron) drops the trap itself, so history and
// the on-screen stack never drift.

import { useEffect, useRef } from "react";

/** Wire the platform back gesture to `onBack` while `depth` layers are open.
 * `onBack` must close exactly one layer (the topmost), like the swipe-down. */
export function useBackGesture(depth: number, onBack: () => void): void {
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;
  const depthRef = useRef(depth);
  depthRef.current = depth;
  const hasTrap = useRef(false);
  // True only while we unwind our own trap after a UI-driven close, so the
  // popstate it raises doesn't double-close the next layer down.
  const reconciling = useRef(false);

  useEffect(() => {
    if (depth > 0 && !hasTrap.current) {
      hasTrap.current = true;
      window.history.pushState({ jbBack: true }, "");
    } else if (depth === 0 && hasTrap.current) {
      // The last layer was closed from the UI, not the OS back — the trap is
      // still in history, so drop it (muting our own popstate handler).
      hasTrap.current = false;
      reconciling.current = true;
      window.history.back();
    }
  }, [depth]);

  useEffect(() => {
    function onPop() {
      if (reconciling.current) {
        reconciling.current = false;
        return;
      }
      if (depthRef.current === 0) return; // nothing open: let the platform exit
      // The OS back consumed our trap. Re-arm one while deeper layers remain so
      // the next back closes those too; then close the top layer.
      if (depthRef.current > 1) {
        window.history.pushState({ jbBack: true }, "");
      } else {
        hasTrap.current = false;
      }
      onBackRef.current();
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
}
