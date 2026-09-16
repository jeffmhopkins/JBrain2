// When the PWA asks the server whether a new build exists.
//
// `registerType: "autoUpdate"` takes care of the rest — the new worker installs, skips
// waiting, and reloads the page — but only once something CALLS `registration.update()`.
// That call used to happen on one schedule: hourly, while foreground. So for up to an
// hour after a deploy the owner was looking at the previous bundle with nothing saying
// so, and he reported a defect that had already been fixed and shipped 30 minutes
// earlier. The bundle on the server was right; his phone had not asked.
//
// A relaunch is not the missing trigger either. In an iOS standalone PWA a
// background/foreground round trip is not a fresh load — the page is restored from the
// page cache, so `autoUpdate`'s own reload never gets its moment. The app comes back via
// exactly the signals `visibility.RESUME_EVENTS` already enumerates for the pollers, for
// exactly the same reason, so the check rides those: **the app asks whenever it comes
// back**, which is the moment the owner is about to look at it.
//
// The hourly timer stays as the backstop for an app left open and never backgrounded.

import { isForeground, onForegroundSignals } from "./visibility";

/** The backstop for a PWA left open for days without ever going background. */
export const UPDATE_CHECK_MS = 60 * 60 * 1000;

/** Several resume signals fire for ONE resume (`pageshow` and `focus` together is
 * routine), so the check is throttled rather than made idempotent by the caller — the
 * request is cheap but it is not free, and a burst of four is still four. */
export const RESUME_THROTTLE_MS = 60 * 1000;

/** What `registerSW` hands back, narrowed to what this needs — so the tests do not have
 * to build a whole `ServiceWorkerRegistration`. */
export interface Updatable {
  update: () => unknown;
}

/** Arm both triggers; returns the teardown (the resume listeners and the timer).
 *
 * `now` is injected so the throttle is testable without a fake clock. */
export function armUpdateChecks(
  registration: Updatable,
  opts: { now?: () => number; setInterval?: typeof setInterval } = {},
): () => void {
  const now = opts.now ?? Date.now;
  const every = opts.setInterval ?? setInterval;
  let last = Number.NEGATIVE_INFINITY;
  const check = (): void => {
    // Re-read the state rather than trusting the event: `focus` and `online` both fire
    // while genuinely backgrounded, and a hidden app has no business asking.
    if (!isForeground()) return;
    const at = now();
    if (at - last < RESUME_THROTTLE_MS) return;
    last = at;
    void registration.update();
  };
  const timer = every(check, UPDATE_CHECK_MS);
  const off = onForegroundSignals(check);
  return () => {
    clearInterval(timer as ReturnType<typeof setInterval>);
    off();
  };
}
