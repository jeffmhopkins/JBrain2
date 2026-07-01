// A LIFO registry of dismissible modal layers (the shared <Sheet>, and any
// future <Dialog>). Sheets render above whatever screen is open, so the single
// back-gesture arbiter in App must close the topmost *sheet* before it climbs
// into the screen stack — otherwise the OS/browser Back gesture closes the
// screen beneath a sheet instead of the sheet itself, while swipe-down closes
// just the sheet, and the two gestures disagree. Sheets self-register here
// while mounted; App folds the count into its overlay depth (so the history
// trap arms for open sheets) and pops the top layer first in closeTopLayer.

import { useEffect, useRef, useSyncExternalStore } from "react";

interface ModalLayer {
  close: () => void;
}

const stack: ModalLayer[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Close the topmost registered modal layer. Returns true when one was closed,
 * so the back-gesture handler can stop before descending into the screen stack. */
export function closeTopModalLayer(): boolean {
  const top = stack[stack.length - 1];
  if (top === undefined) return false;
  top.close();
  return true;
}

/** How many modal layers are open — App adds this to its back-gesture depth so
 * the history trap stays armed while any sheet is up. */
export function useModalLayerCount(): number {
  return useSyncExternalStore(
    subscribe,
    () => stack.length,
    () => stack.length,
  );
}

/** Register `onClose` as the topmost dismissible layer while mounted, so the
 * platform Back gesture pops this sheet before the screen beneath it. The
 * shared <Sheet> mounts only while open, so registration tracks visibility. */
export function useBackLayer(onClose: () => void): void {
  // onClose identity changes each render; keep the latest without re-registering,
  // since re-pushing every render would reorder the stack.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const layer: ModalLayer = { close: () => onCloseRef.current() };
    stack.push(layer);
    emit();
    return () => {
      const index = stack.indexOf(layer);
      if (index !== -1) stack.splice(index, 1);
      emit();
    };
  }, []);
}
