// The PWA's foreground/background signal. Backgrounding the app — switching
// tabs, minimizing, locking the phone, or swapping apps — flips the Page
// Visibility API to "hidden", and a backgrounded app has no business holding
// the server busy. Every recurring poll consults this so it suspends while
// hidden and resumes (with an immediate catch-up) the moment the owner brings
// the app back.
//
// This deliberately tracks visibility, not focus: a fully-visible-but-unfocused
// desktop window (another window has focus, or a second monitor) stays
// "visible" and keeps polling — suspending a window the owner can still see
// would be worse. `blur`/`pagehide`/`freeze` are intentionally not used here.

import { useEffect, useRef, useState } from "react";

/** True when the app is in the foreground (or off-DOM, e.g. during SSR/tests). */
export function isForeground(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/** Reactive foreground state for effects that arm/disarm a poll declaratively:
 * flipping false tears the interval down, flipping true re-runs the effect so
 * it fires an immediate fetch before re-arming. */
export function useForeground(): boolean {
  const [foreground, setForeground] = useState(isForeground);
  useEffect(() => {
    const onChange = () => setForeground(isForeground());
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return foreground;
}

/** Live foreground flag for imperative `setInterval` callbacks, which capture
 * their closure once: a ref lets the tick read the current value (and skip its
 * request while hidden) without re-arming the timer. */
export function useForegroundRef(): React.MutableRefObject<boolean> {
  const ref = useRef(isForeground());
  useEffect(() => {
    const onChange = () => {
      ref.current = isForeground();
    };
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return ref;
}
