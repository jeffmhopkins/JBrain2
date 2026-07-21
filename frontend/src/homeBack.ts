// The home conversation surface (Full Brain / Research) owns internal layers the
// App-level back arbiter can't see: the Sessions/Proposals panels and an open Proposal.
// They sit at the BOTTOM of the z-stack — on the home surface, beneath every card, sheet,
// and reading layer — so the back gesture must climb them only after everything stacked
// above home is gone. HomeScreen registers a closer plus its remaining depth here; App
// folds the depth into its overlay depth (so `closeTopLayer` runs while a panel is up)
// and calls the closer LAST, once no higher layer remains.
//
// A single base slot (not the LIFO stack backLayers.ts keeps for sheets) — there is only
// ever one home surface, and its layers unwind within its own closer.

import { useEffect, useRef } from "react";
import { useSyncExternalStore } from "react";

let depth = 0;
let close: () => boolean = () => false;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

/** Register the home surface's back handler + how many of its own layers are open, while
 * mounted. `close` pops ONE of those layers and returns whether it did. */
export function useRegisterHomeBack(homeDepth: number, close: () => boolean): void {
  // close identity changes each render; keep the latest in a ref so the effect only
  // re-runs (and re-notifies) when the depth actually changes.
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    setHomeBack(homeDepth, () => closeRef.current());
    return () => setHomeBack(0, () => false);
  }, [homeDepth]);
}

function setHomeBack(nextDepth: number, nextClose: () => boolean): void {
  depth = nextDepth;
  close = nextClose;
  emit();
}

/** How many of the home surface's own layers are open — App adds this to its back-gesture
 * depth so `closeTopLayer` runs (and the trap keeps trapping) while a panel is up. */
export function useHomeBackDepth(): number {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => depth,
    () => depth,
  );
}

/** Close the topmost open layer of the home surface. Returns true when one was closed, so
 * the back-gesture handler can stop before falling through to the app exit no-op. */
export function closeHomeBackLayer(): boolean {
  return close();
}
