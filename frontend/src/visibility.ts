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

/** Everything that can mean "the app is back".
 *
 *  `visibilitychange` alone is not enough, and that is the bug this list fixes. In an iOS
 *  standalone PWA a background/foreground round trip frequently delivers only `pageshow`
 *  (the page is restored from the page cache) — no visibility event at all — and a resumed
 *  app often comes back on a different network, where `online` is the only signal. Missing
 *  the resume is not cosmetic: hostVitals learned this first (its socket was already dead
 *  by then, so the top bar sat on dashes until the whole app was restarted), and the hooks
 *  below learned it next — a hidden-flip that fired without a matching visible-flip left
 *  every `useForeground`-gated poll torn down, which is how the vitals screen came back
 *  from a long absence frozen.
 *
 *  These are deliberately additive to `visibilitychange`, not a replacement: several of
 *  them fire for the same resume, so anything listening must be idempotent. They are also
 *  resume-side only — going hidden is still detected by `visibilitychange` alone, and the
 *  handlers re-read `visibilityState` rather than trusting the event, so a `focus` or
 *  `online` while genuinely backgrounded changes nothing. */
export const RESUME_EVENTS = ["pageshow", "focus", "online"] as const;

/** True when the app is in the foreground (or off-DOM, e.g. during SSR/tests). */
export function isForeground(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/** Wire `onChange` to every signal that can move the foreground state; returns the
 *  teardown. Shared by both hooks so neither can drift back to visibility-only. */
function onForegroundSignals(onChange: () => void): () => void {
  document.addEventListener("visibilitychange", onChange);
  for (const event of RESUME_EVENTS) window.addEventListener(event, onChange);
  return () => {
    document.removeEventListener("visibilitychange", onChange);
    for (const event of RESUME_EVENTS) window.removeEventListener(event, onChange);
  };
}

/** Reactive foreground state for effects that arm/disarm a poll declaratively:
 * flipping false tears the interval down, flipping true re-runs the effect so
 * it fires an immediate fetch before re-arming. */
export function useForeground(): boolean {
  const [foreground, setForeground] = useState(isForeground);
  useEffect(() => onForegroundSignals(() => setForeground(isForeground())), []);
  return foreground;
}

/** Live foreground flag for imperative `setInterval` callbacks, which capture
 * their closure once: a ref lets the tick read the current value (and skip its
 * request while hidden) without re-arming the timer. */
export function useForegroundRef(): React.MutableRefObject<boolean> {
  const ref = useRef(isForeground());
  useEffect(
    () =>
      onForegroundSignals(() => {
        ref.current = isForeground();
      }),
    [],
  );
  return ref;
}
