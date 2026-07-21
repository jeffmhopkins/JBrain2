// Native back = climb one level, the same as the swipe-down every stacked layer
// already honors (docs/reference/DESIGN.md navigation tree). Two hosts drive it:
//
// 1. The PWA / browser, which has no URL router — so the OS/browser back gesture would
//    otherwise leave the PWA. We mirror the open-layer stack into the History API and keep
//    one permanent "root trap" beneath it (depth + 1 entries above the base), so backing
//    out of any layer is a native history pop that lands on another of our entries — never
//    the base — and only a bare-screen back reaches the root trap, which we re-arm.
//
// 2. The native owner app (an Android WebView), which owns the system back button itself:
//    it appends a UA marker and, on back, calls `window.__jbrainBack()` and BACKGROUNDS the
//    app (never exits) when nothing was open. There the History API trap is skipped — the
//    native handler is authoritative and a trap would only fight it — and this bridge is
//    the single source of truth.

import { useEffect, useRef } from "react";

declare global {
  interface Window {
    /** Close the topmost open layer; returns whether one was closed. Published for a
     * native host to drive the system back button (see host note above). */
    __jbrainBack?: (() => boolean) | undefined;
  }
}

/** Whether we're running inside the native owner WebView, which drives back through the
 * `__jbrainBack` bridge rather than the History API. Checked live (not a module constant)
 * so tests can stub the user agent. */
function nativeHost(): boolean {
  return typeof navigator !== "undefined" && / JBrainOwner\//.test(navigator.userAgent);
}

/** Wire the platform back gesture to `onBack` while `depth` layers are open. `onBack` must
 * close exactly one layer (the topmost), like the swipe-down. Back never exits the app: at
 * depth 0 the gesture is consumed (browser: root trap re-arms; native: app backgrounds). */
export function useBackGesture(depth: number, onBack: () => void): void {
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;
  const depthRef = useRef(depth);
  depthRef.current = depth;
  // How many entries we've pushed above the app's base entry (browser path). Kept in step
  // with `depth + 1`: one per open layer, plus the permanent root trap beneath them.
  const armed = useRef(0);
  // Popstate events our own `history.go()` will raise and must ignore — a UI-driven close
  // unwinds history to match and must not also climb a layer. A traversal fires a single
  // popstate regardless of its length, so we mute one per `go()`.
  const muted = useRef(0);

  // Publish the imperative back bridge for a native host: close the top layer if one is
  // open, and report whether we did — the host backgrounds the app (never exits) when we
  // return false. Harmless in a browser, where nothing calls it.
  useEffect(() => {
    window.__jbrainBack = () => {
      if (depthRef.current > 0) {
        onBackRef.current();
        return true;
      }
      return false;
    };
    return () => {
      window.__jbrainBack = undefined;
    };
  }, []);

  // Mirror the layer depth into history after EVERY render — not just on depth changes — so
  // a close that swaps one layer for another (Tasks' return-to-card, where depth stays
  // constant) still tops the pushed entries back up. Idempotent: it touches history only
  // when the count has drifted from `depth + 1`. Browser path only — a native host owns the
  // back button, so the trap is skipped there to avoid fighting it.
  useEffect(() => {
    if (nativeHost()) return;
    const target = depth + 1;
    if (armed.current < target) {
      for (let i = armed.current; i < target; i++) {
        window.history.pushState({ jbBack: true }, "");
      }
      armed.current = target;
    } else if (armed.current > target) {
      const surplus = armed.current - target;
      armed.current = target;
      muted.current += 1;
      window.history.go(-surplus);
    }
  });

  useEffect(() => {
    if (nativeHost()) return;
    function onPop() {
      if (muted.current > 0) {
        muted.current -= 1;
        return;
      }
      // The OS back popped one of our entries.
      armed.current = Math.max(0, armed.current - 1);
      if (depthRef.current > 0) {
        // Climb one layer; the sync effect tops the entries back up for the new depth
        // (including the constant-depth swap, where it re-pushes the entry just consumed).
        onBackRef.current();
      } else {
        // Popped the root trap at the bare main screen — re-arm it so back stays in the app.
        window.history.pushState({ jbBack: true }, "");
        armed.current += 1;
      }
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
}
