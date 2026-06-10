// Capture location (docs/DESIGN.md "Capture location"): a Settings toggle,
// on by default. While on, the app keeps a warm geolocation fix (refreshed at
// start and when the tab becomes visible) and a send attaches the coordinates
// only when the fix is under 2 minutes old — capture never waits for GPS.
// Permission denial just means location-less notes.

export interface CaptureCoords {
  latitude: number;
  longitude: number;
  accuracy_m: number;
}

const STORAGE_KEY = "jbrain.captureLocation";
export const FIX_MAX_AGE_MS = 2 * 60 * 1000;

interface WarmFix extends CaptureCoords {
  at: number;
}

let lastFix: WarmFix | null = null;

export function isLocationCaptureEnabled(): boolean {
  return localStorage.getItem(STORAGE_KEY) !== "off";
}

export function setLocationCaptureEnabled(on: boolean): void {
  localStorage.setItem(STORAGE_KEY, on ? "on" : "off");
  if (on) warmFix();
  else lastFix = null;
}

/** Low-accuracy is fine — the fix only tags where a note was written. */
export function warmFix(): void {
  if (!isLocationCaptureEnabled()) return;
  if (typeof navigator === "undefined" || !navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      lastFix = {
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracy_m: pos.coords.accuracy,
        at: Date.now(),
      };
    },
    () => {
      // Denied or unavailable — notes go out location-less, silently.
    },
    { maximumAge: 60_000, timeout: 30_000 },
  );
}

/** Coordinates for a send, or null when the warm fix is stale/off/missing. */
export function freshCoords(now: number = Date.now()): CaptureCoords | null {
  if (!isLocationCaptureEnabled() || lastFix === null) return null;
  if (now - lastFix.at > FIX_MAX_AGE_MS) return null;
  const { latitude, longitude, accuracy_m } = lastFix;
  return { latitude, longitude, accuracy_m };
}

export function initLocationCapture(): void {
  warmFix();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") warmFix();
  });
}
