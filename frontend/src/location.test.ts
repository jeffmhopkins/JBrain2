import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  FIX_MAX_AGE_MS,
  freshCoords,
  isLocationCaptureEnabled,
  setLocationCaptureEnabled,
  warmFix,
} from "./location";

function stubGeolocation(coords: { latitude: number; longitude: number; accuracy: number }) {
  Object.defineProperty(navigator, "geolocation", {
    configurable: true,
    value: {
      getCurrentPosition: (ok: PositionCallback) =>
        ok({ coords, timestamp: Date.now() } as GeolocationPosition),
    },
  });
}

function stubDeniedGeolocation() {
  Object.defineProperty(navigator, "geolocation", {
    configurable: true,
    value: {
      getCurrentPosition: (_ok: PositionCallback, err?: PositionErrorCallback) =>
        err?.({ code: 1, message: "denied" } as GeolocationPositionError),
    },
  });
}

describe("capture location", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    // Clear any fix left from a previous test, then re-enable the default.
    setLocationCaptureEnabled(false);
    localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("defaults to enabled with nothing persisted", () => {
    expect(isLocationCaptureEnabled()).toBe(true);
  });

  it("attaches a fix under 2 minutes old", () => {
    stubGeolocation({ latitude: 47.61, longitude: -122.33, accuracy: 40 });
    warmFix();
    expect(freshCoords()).toEqual({ latitude: 47.61, longitude: -122.33, accuracy_m: 40 });
  });

  it("drops the fix once it is older than 2 minutes — never blocks the send", () => {
    stubGeolocation({ latitude: 47.61, longitude: -122.33, accuracy: 40 });
    warmFix();
    vi.advanceTimersByTime(FIX_MAX_AGE_MS);
    expect(freshCoords()).not.toBeNull();
    vi.advanceTimersByTime(1);
    expect(freshCoords()).toBeNull();
  });

  it("returns nothing while the toggle is off, even with a warm fix", () => {
    stubGeolocation({ latitude: 1, longitude: 2, accuracy: 5 });
    warmFix();
    setLocationCaptureEnabled(false);
    expect(freshCoords()).toBeNull();
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("off");
  });

  it("denied permission silently produces location-less sends", () => {
    stubDeniedGeolocation();
    warmFix();
    expect(freshCoords()).toBeNull();
  });

  it("does not warm a fix while disabled", () => {
    setLocationCaptureEnabled(false);
    stubGeolocation({ latitude: 1, longitude: 2, accuracy: 5 });
    warmFix();
    setLocationCaptureEnabled(true); // re-enabling warms a fresh fix instead
    expect(localStorage.getItem("jbrain.captureLocation")).toBe("on");
  });
});
