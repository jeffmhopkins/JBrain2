// Native back = climb one level, the same as the swipe-down every stacked layer
// already honors (docs/reference/DESIGN.md navigation tree). The app keeps no URL router,
// so the OS/browser back gesture would otherwise leave the PWA instead of popping a layer.
//
// We mirror the open-layer stack into the History API and keep ONE extra permanent "root
// trap" beneath it, so there are always `depth + 1` of our entries above the app's base
// entry. Backing out of any layer is then a NATIVE history pop that lands on another of
// our entries — never on the base — so the platform never treats it as "nothing left,
// leave the app". Only a back at the bare main screen (depth 0) reaches the root trap,
// which we re-arm in the popstate handler so even that stays in the app.
//
// Why one entry PER layer, not a single shared trap: with a lone trap the launcher (and a
// bare chat) sat exactly one entry above the base, so backing out of it landed on the base
// and relied on synchronously re-pushing a trap inside the popstate handler — a race
// Android's gesture/predictive back does not reliably honor, so the app would exit. A real
// entry per layer keeps every layer-back a full step above the base.

import { useEffect, useRef } from "react";

/** Wire the platform back gesture to `onBack` while `depth` layers are open. `onBack` must
 * close exactly one layer (the topmost), like the swipe-down. Back never exits the app: at
 * depth 0 the gesture is consumed and does nothing. */
export function useBackGesture(depth: number, onBack: () => void): void {
  const onBackRef = useRef(onBack);
  onBackRef.current = onBack;
  const depthRef = useRef(depth);
  depthRef.current = depth;
  // How many entries we've pushed above the app's base entry. Kept in step with
  // `depth + 1`: one per open layer, plus the permanent root trap beneath them.
  const armed = useRef(0);
  // Popstate events our own `history.go()` will raise and must ignore — a UI-driven close
  // unwinds history to match and must not also climb a layer. A traversal fires a single
  // popstate regardless of its length, so we mute one per `go()`.
  const muted = useRef(0);

  // Mirror the layer depth into history after EVERY render — not just on depth changes — so
  // a close that swaps one layer for another (Tasks' return-to-card, where depth stays
  // constant) still tops the pushed entries back up. Idempotent: it touches history only
  // when the count has actually drifted from `depth + 1`.
  useEffect(() => {
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
